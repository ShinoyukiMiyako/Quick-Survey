from datetime import datetime
from typing import Optional
import re
from pydantic import BaseModel, Field, field_validator


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
    """题目验证规则"""
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    max_images: Optional[int] = None


class QuestionConditionSchema(BaseModel):
    """条件显示规则

    用于实现分支逻辑：根据某道题的答案决定是否显示当前题目
    例如："是否游玩过BA？" 选"是"则显示BA相关题目
    """
    depends_on: int  # 依赖题的 question_id（稳定引用，不随题序/编辑变化）— 前后端一致
    show_when: str | list[str]  # 触发显示的答案值（支持单值或多值）


class QuestionCreate(BaseModel):
    """创建问题"""
    title: str = Field(..., min_length=1, max_length=1000)
    description: Optional[str] = None
    type: str = Field(..., pattern="^(single|multiple|boolean|text|image)$")
    options: Optional[list[QuestionOptionSchema]] = None
    is_required: bool = True
    is_pinned: bool = False  # 是否保留（随机抽题时始终出现）
    order: int = 0
    validation: Optional[QuestionValidationSchema] = None
    condition: Optional[QuestionConditionSchema] = None  # 条件显示规则
    role: Optional[str] = Field(None, pattern="^(player_name|qq)$")  # 语义标记: 标记为系统字段 (玩家名/QQ)


class QuestionUpdate(BaseModel):
    """更新问题"""
    title: Optional[str] = Field(None, min_length=1, max_length=1000)
    description: Optional[str] = None
    type: Optional[str] = Field(None, pattern="^(single|multiple|boolean|text|image)$")
    options: Optional[list[QuestionOptionSchema]] = None
    is_required: Optional[bool] = None
    is_pinned: Optional[bool] = None  # 是否保留
    order: Optional[int] = None
    validation: Optional[QuestionValidationSchema] = None
    condition: Optional[QuestionConditionSchema] = None  # 条件显示规则
    role: Optional[str] = Field(None, pattern="^(player_name|qq)$")  # 语义标记


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
    cover_url: Optional[str] = Field(None, max_length=512)
    icon: Optional[str] = Field(None, max_length=64)
    theme_color: Optional[str] = Field(None, max_length=16)
    summary: Optional[str] = Field(None, max_length=255)
    estimated_minutes: Optional[int] = Field(None, ge=1)
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
    player_name: str
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
    player_name: str
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


# ==================== 文件上传 ====================

class UploadResponse(BaseModel):
    """文件上传响应"""
    filename: str
    stored_name: str
    url: str
    size: int
    mime_type: str
