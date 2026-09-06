from datetime import datetime
from typing import Optional
import re
from pydantic import BaseModel, Field, field_validator, model_validator


def _check_question_type(value: Optional[str]) -> Optional[str]:
    """题型必须在 question_types 注册表内; 题型清单的唯一事实源就是那份注册表。

    这里在函数体内 import 而不是模块级 import: question_types.py 本身零依赖, 但导入它必然先
    执行 app/services/__init__.py, 而那个包 __init__ 会连带 import survey.py, survey.py 又
    反向 `from app.schemas import ...` —— api/*.py 一律先 import app.schemas, 于是模块级引用
    会在 app.schemas 初始化到一半时撞成循环导入, 整个应用起不来。校验只在这一刻需要注册表,
    推迟到运行期取即可, 既不手抄题型字面量, 也不把包级环带进来。
    """
    from app.services.question_types import QUESTION_TYPE_PATTERN

    if value is None:
        return value
    if not re.match(QUESTION_TYPE_PATTERN, value):
        raise ValueError(f"不支持的题型: {value}")
    return value


# ==================== 通用响应 ====================

class ApiResponse(BaseModel):
    """统一 API 响应格式"""
    success: bool
    data: Optional[dict] = None
    error: Optional[dict] = None
    message: Optional[str] = None


class PaginatedResponse(BaseModel):
    """分页响应"""
    items: list
    page: int
    size: int
    total: int
    pages: int


# ==================== 问题相关 ====================

class QuestionOptionSchema(BaseModel):
    """题目选项"""
    value: str
    label: str


class QuestionValidationSchema(BaseModel):
    """题目验证规则 (按题型各取所需, 未配置的项为 None 即不限制)"""
    min_length: Optional[int] = None  # text / short_text 最少字数 (按去首尾空白后的长度算)
    max_length: Optional[int] = None  # text / short_text 最多字数
    max_images: Optional[int] = None  # image 最多张数
    max_files: Optional[int] = None  # file 最多个数, 缺省 3
    # file 允许的扩展名 (小写带点, 如 [".ysm", ".zip"]); 不配则只受站点级白名单约束
    allowed_extensions: Optional[list[str]] = None
    min_value: Optional[float] = None  # number 下限 (闭区间)
    max_value: Optional[float] = None  # number 上限 (闭区间)
    max_rating: Optional[int] = None  # rating 满分, 缺省按 5 分制
    min_date: Optional[str] = None  # date 最早可选日期, "YYYY-MM-DD"
    max_date: Optional[str] = None  # date 最晚可选日期, "YYYY-MM-DD"


# 条件比较值: 标量或标量数组。求值时统一转成字符串比较, 故这里只把形态放宽到实际会出现的几种,
# 不做类型收窄 —— 前端 number/rating 题回传的是数字, 多选回传数组, 都得能过。
ConditionValue = str | bool | int | float | list[str | bool | int | float]


class ConditionRuleSchema(BaseModel):
    """条件规则 v2 的单条规则: 某道依赖题的答案与期望值按 operator 比较"""
    question_id: int  # 依赖题的 question_id (稳定引用, 不随题序/编辑变化)
    operator: str = Field("eq", pattern="^(eq|neq|in|not_in|contains|gt|lt|answered|not_answered)$")
    value: Optional[ConditionValue] = None  # answered / not_answered 忽略本字段


class QuestionConditionSchema(BaseModel):
    """条件显示规则 (新旧两种形态都必须收得下)

    用于实现分支逻辑：根据某道题的答案决定是否显示当前题目
    例如："是否游玩过BA？" 选"是"则显示BA相关题目

    v2 形态 {action, match, rules} 支持多条规则与显示/隐藏取反; 旧形态
    {depends_on, show_when} 是存量库里全部条件的写法, 新旧都得能反序列化 (QuestionResponse
    也走这个模型), 故所有字段可选, 完整性交给 model_validator 把关。
    """
    # v2 形态; action 缺省 show, match 缺省 all (归一化在 services/conditions.py)
    action: Optional[str] = Field(None, pattern="^(show|hide)$")
    match: Optional[str] = Field(None, pattern="^(all|any)$")
    rules: Optional[list[ConditionRuleSchema]] = None
    # 旧形态 (存量数据与旧版前端)
    depends_on: Optional[int] = None  # 依赖题的 question_id
    show_when: Optional[str | list[str]] = None  # 触发显示的答案值（支持单值或多值）

    @model_validator(mode="after")
    def check_shape(self):
        """两种形态必须至少完整落在其中一种上。

        半截条件 (只有 depends_on 没有 show_when, 或 rules 为空数组) 写进库后, 条件引擎
        会把它当无效条件丢弃, 题目变成无条件常显 —— 分支静默失效比直接报错难查得多。
        """
        if self.rules:
            return self
        if self.depends_on is not None and self.show_when is not None:
            return self
        raise ValueError("条件规则不完整: 需提供非空 rules, 或同时提供 depends_on 与 show_when")


class QuestionCreate(BaseModel):
    """创建问题"""
    title: str = Field(..., min_length=1, max_length=1000)
    description: Optional[str] = None
    type: str
    options: Optional[list[QuestionOptionSchema]] = None
    is_required: bool = True
    is_pinned: bool = False  # 是否保留（随机抽题时始终出现）
    order: int = 0
    validation: Optional[QuestionValidationSchema] = None
    condition: Optional[QuestionConditionSchema] = None  # 条件显示规则
    role: Optional[str] = Field(None, pattern="^(player_name|qq)$")  # 语义标记: 标记为系统字段 (玩家名/QQ)

    @field_validator('type')
    @classmethod
    def check_type(cls, v: str) -> str:
        return _check_question_type(v)


class QuestionUpdate(BaseModel):
    """更新问题"""
    title: Optional[str] = Field(None, min_length=1, max_length=1000)
    description: Optional[str] = None
    type: Optional[str] = None
    options: Optional[list[QuestionOptionSchema]] = None
    is_required: Optional[bool] = None
    is_pinned: Optional[bool] = None  # 是否保留
    order: Optional[int] = None
    validation: Optional[QuestionValidationSchema] = None
    condition: Optional[QuestionConditionSchema] = None  # 条件显示规则
    role: Optional[str] = Field(None, pattern="^(player_name|qq)$")  # 语义标记

    @field_validator('type')
    @classmethod
    def check_type(cls, v: Optional[str]) -> Optional[str]:
        return _check_question_type(v)


class QuestionResponse(BaseModel):
    """问题响应"""
    id: int
    title: str
    description: Optional[str]
    type: str
    options: Optional[list[QuestionOptionSchema]]
    is_required: bool
    is_pinned: bool  # 是否保留
    order: int
    validation: Optional[QuestionValidationSchema]
    condition: Optional[QuestionConditionSchema] = None  # 条件显示规则
    role: Optional[str] = None  # 语义标记 (玩家名/QQ), 前端据此识别系统字段题

    class Config:
        from_attributes = True


# ==================== 问卷相关 ====================

class SurveyCreate(BaseModel):
    """创建问卷"""
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    is_random: bool = False
    random_count: Optional[int] = Field(None, ge=1)
    # 门户编排: 栏目(whitelist/collection) 与卡片展示
    category: str = Field("whitelist", pattern="^(whitelist|collection)$")
    visibility: str = Field("public", pattern="^(public|unlisted|private)$")
    # 面板的"设置"页在新建时就能选草稿/发布; 这里不收就会被 pydantic 静默丢弃, 表现为
    # "建的时候选了草稿, 建出来却是已发布"
    status: str = Field("published", pattern="^(draft|published|archived)$")
    cover_url: Optional[str] = Field(None, max_length=512)
    icon: Optional[str] = Field(None, max_length=64)
    theme_color: Optional[str] = Field(None, max_length=16)
    summary: Optional[str] = Field(None, max_length=255)
    estimated_minutes: Optional[int] = Field(None, ge=1)
    # 开放窗口与提交配额 (不传即不限)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    max_submissions: Optional[int] = Field(None, ge=1)
    max_submissions_per_ip: Optional[int] = Field(None, ge=1)
    # 合规声明与自定义文案
    require_consent: Optional[bool] = None
    privacy_notice: Optional[str] = None
    closed_message: Optional[str] = Field(None, max_length=500)
    success_message: Optional[str] = Field(None, max_length=500)
    # 场景动作开关: 默认为 None 而非 True/False —— create_survey 按 category 播种默认值,
    # 只有面板显式传了才覆盖 (靠 exclude_unset 区分"没传"与"传了 false")
    review_required: Optional[bool] = None
    action_add_whitelist: Optional[bool] = None
    action_issue_code: Optional[bool] = None
    action_notify_group: Optional[bool] = None
    action_webhook: Optional[bool] = None
    webhook_url: Optional[str] = Field(None, max_length=512)
    # 通知投递群号 (QQ 群号, 不传/null = 用插件默认审核群并 @ 提交者本人)
    notify_group_id: Optional[int] = Field(None, ge=1)
    questions: list[QuestionCreate] = []


class SurveyUpdate(BaseModel):
    """更新问卷 (exclude_unset: 仅更新显式提供的字段)"""
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    is_random: Optional[bool] = None
    random_count: Optional[int] = Field(None, ge=1)
    # 门户编排
    sort_order: Optional[int] = Field(None, ge=0)
    is_pinned: Optional[bool] = None
    category: Optional[str] = Field(None, pattern="^(whitelist|collection)$")
    visibility: Optional[str] = Field(None, pattern="^(public|unlisted|private)$")
    status: Optional[str] = Field(None, pattern="^(draft|published|archived)$")
    cover_url: Optional[str] = Field(None, max_length=512)
    icon: Optional[str] = Field(None, max_length=64)
    theme_color: Optional[str] = Field(None, max_length=16)
    summary: Optional[str] = Field(None, max_length=255)
    estimated_minutes: Optional[int] = Field(None, ge=1)
    # 场景动作开关 (提交后行为)
    review_required: Optional[bool] = None
    action_add_whitelist: Optional[bool] = None
    action_issue_code: Optional[bool] = None
    action_notify_group: Optional[bool] = None
    action_webhook: Optional[bool] = None
    webhook_url: Optional[str] = Field(None, max_length=512)
    # 通知投递群号; 与下面的窗口/配额同理, 显式传 null 才是清空 (回退默认审核群)
    notify_group_id: Optional[int] = Field(None, ge=1)
    # 开放窗口与提交配额。这几项要能被"清空": update_survey 用 exclude_unset 取差量,
    # 显式传 null 时键仍在 dump 里, 会把列置回 NULL(不限); 整个键不传才是保持原样。
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    max_submissions: Optional[int] = Field(None, ge=1)
    max_submissions_per_ip: Optional[int] = Field(None, ge=1)
    # 合规声明与自定义文案
    require_consent: Optional[bool] = None
    privacy_notice: Optional[str] = None
    closed_message: Optional[str] = Field(None, max_length=500)
    success_message: Optional[str] = Field(None, max_length=500)
    # 非列字段: 明文访问口令。update_survey 会先把它从 dump 里 pop 掉再哈希写入
    # access_password_hash, 绝不会 setattr 到 Survey 上。传空串/null 表示清除口令。
    access_password: Optional[str] = None


class SurveyReorderItem(BaseModel):
    """批量重排的单项"""
    id: int
    sort_order: int = Field(..., ge=0)


class SurveyReorderRequest(BaseModel):
    """批量重排问卷展示顺序"""
    orders: list[SurveyReorderItem]


class SurveyResponse(BaseModel):
    """问卷响应（列表）"""
    id: int
    title: str
    description: Optional[str]
    code: str
    is_active: bool
    is_random: bool
    random_count: Optional[int]
    question_count: int = 0
    submission_count: int = 0
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class SurveyDetailResponse(BaseModel):
    """问卷详情响应"""
    id: int
    title: str
    description: Optional[str]
    code: str
    is_active: bool
    is_random: bool
    random_count: Optional[int]
    questions: list[QuestionResponse]
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ==================== 公开问卷（玩家端）====================

class PublicSurveyResponse(BaseModel):
    """公开问卷响应（给玩家看的）"""
    title: str
    description: Optional[str]
    questions: list[QuestionResponse]


class SurveyUnlockRequest(BaseModel):
    """口令卷解锁请求 (校验通过才回传题目, 未解锁时详情接口的 questions 恒为空)"""
    password: str


# ==================== 提交相关 ====================

class AnswerSubmit(BaseModel):
    """提交答案"""
    question_id: int
    content: dict  # 根据题型不同，格式不同


class SubmissionCreate(BaseModel):
    """创建提交"""
    # 可空: 优先由后端从 role=player_name 题抽取; 顶层字段保留向后兼容旧前端
    player_name: Optional[str] = Field(None, max_length=64)
    answers: list[AnswerSubmit]
    # 安全相关字段
    turnstile_token: Optional[str] = None  # Cloudflare Turnstile token
    start_time: Optional[float] = None  # 开始填写时间戳（秒）
    # 口令卷的明文访问口令: 玩家解锁后前端一直带着它, 提交时后端再验一次, 防绕过解锁接口直接 POST
    access_password: Optional[str] = None
    consent: Optional[bool] = None  # 卷开启 require_consent 时必须为 True 才受理

    @field_validator('player_name')
    @classmethod
    def sanitize_player_name(cls, v: Optional[str]) -> Optional[str]:
        """清理玩家名称中的潜在恶意字符 (可空: 由后端从题目抽取时顶层为空)。

        Note: SQL 注入由 SQLAlchemy 参数化查询防御，无需在此处过滤 ';' / '--'
        （那只会破坏合法名字如 "O'Brien"，并不提供真实保护）。
        XSS 应在前端渲染时 escape，这里仅作最后一道防线移除明显的脚本片段。
        """
        if v is None:
            return v
        v = re.sub(r'<[^>]*>', '', v)
        v = re.sub(r'on\w+\s*=', '', v, flags=re.IGNORECASE)
        return v.strip()


class AnswerResponse(BaseModel):
    """答案响应"""
    id: int
    question_id: int
    question_title: str = ""
    question_type: str = ""
    content: dict
    
    class Config:
        from_attributes = True


class SubmissionListResponse(BaseModel):
    """提交列表响应"""
    id: int
    survey_id: int
    survey_title: str = ""
    # 可空: 匿名收集表没有 role=player_name 题, 提交后该列就是 NULL, 声明成必填 str 会让序列化直接抛
    player_name: Optional[str] = None
    qq: Optional[str] = None
    status: str
    created_at: datetime
    reviewed_at: Optional[datetime]

    class Config:
        from_attributes = True


class SubmissionDetailResponse(BaseModel):
    """提交详情响应"""
    id: int
    survey_id: int
    survey_title: str = ""
    # 可空: 同 SubmissionListResponse, 匿名收集表的提交没有玩家名
    player_name: Optional[str] = None
    qq: Optional[str] = None
    ip_address: Optional[str]
    status: str
    review_note: Optional[str]
    answers: list[AnswerResponse]
    created_at: datetime
    reviewed_at: Optional[datetime]
    reviewed_by: Optional[int]
    
    class Config:
        from_attributes = True


class SubmissionReview(BaseModel):
    """审核提交"""
    status: str = Field(..., pattern="^(approved|rejected)$")
    review_note: Optional[str] = None


class BulkReviewRequest(BaseModel):
    """批量审核提交 (面板勾选多条后一次性通过/拒绝)"""
    ids: list[int] = Field(..., min_length=1)
    status: str = Field(..., pattern="^(approved|rejected)$")
    review_note: Optional[str] = None  # 整批共用一条备注, 拒绝时用于说明原因


# ==================== 文件上传 ====================

class UploadResponse(BaseModel):
    """文件上传响应"""
    filename: str
    stored_name: str
    url: str
    size: int
    mime_type: str
