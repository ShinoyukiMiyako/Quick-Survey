from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    """获取当前 UTC 时间（兼容 Python 3.12+）"""
    return datetime.now(timezone.utc)


class Survey(Base):
    """问卷模板"""
    __tablename__ = "surveys"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    
    # 基本信息
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)  # 访问短码
    
    # 配置
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # 是否启用
    is_random: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否随机题目
    random_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 随机抽取题目数量

    # 门户编排 (多表单入口: 可选 + 可排序 + 分栏)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # 展示排序位 (升序)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)  # 置顶 (置顶组优先, 组内再按 sort_order)
    # 栏目/场景: whitelist=MC白名单卷, collection=其它收集表; 面板据此分栏, 也是场景解耦锚点
    category: Mapped[str] = mapped_column(String(32), default="whitelist")
    # 可见性: public=进门户列表, unlisted=仅凭链接可填, private=仅管理端预览
    visibility: Mapped[str] = mapped_column(String(16), default="public")
    # 生命周期: draft=草稿, published=已发布, archived=已归档; 与 is_active 并存, 门户只列 published
    status: Mapped[str] = mapped_column(String(16), default="published")

    # 门户卡片展示 (可选, 运营配置)
    cover_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)  # 封面图 URL
    icon: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 图标 (emoji 或图标名)
    theme_color: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # 主题色
    summary: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # 卡片简介 (短于 description)
    estimated_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 预计耗时(分钟)

    # 场景动作 (提交后行为按表单可配; whitelist 默认全开, collection 默认全关 —— 见 create_survey)
    # 模型默认全 True 保持白名单行为; 收集表在 create_survey 里按 category 播种为 False。
    review_required: Mapped[bool] = mapped_column(Boolean, default=True)  # 是否人工审核 (关=免审, 提交即终态)
    action_add_whitelist: Mapped[bool] = mapped_column(Boolean, default=True)  # 通过时加 MC 白名单
    action_issue_code: Mapped[bool] = mapped_column(Boolean, default=True)  # 允许通过后领取注册码
    action_notify_group: Mapped[bool] = mapped_column(Boolean, default=True)  # 提交/审核入审核群通知队列

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 创建者 admin_id
    
    # 关系
    questions: Mapped[list["Question"]] = relationship("Question", back_populates="survey", cascade="all, delete-orphan")
    submissions: Mapped[list["Submission"]] = relationship("Submission", back_populates="survey", cascade="all, delete-orphan")


class Question(Base):
    """问题"""
    __tablename__ = "questions"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False)
    
    # 题目内容
    title: Mapped[str] = mapped_column(Text, nullable=False)  # 题目标题
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 题目描述
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # 类型: single, multiple, boolean, text, image
    
    # 选项 (JSON 数组，用于单选/多选题)
    # 格式: [{"value": "A", "label": "选项A"}, ...]
    options: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    # 配置
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)  # 是否必填
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否保留（随机抽题时始终出现）
    order: Mapped[int] = mapped_column(Integer, default=0)  # 排序
    
    # 验证规则 (JSON)
    # 格式: {"min_length": 10, "max_length": 500, "max_images": 3}
    validation: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    # 条件显示规则 (JSON)
    # 格式: {"depends_on": 问题ID, "show_when": "答案值" 或 ["值1", "值2"]}
    # 用于实现分支逻辑：根据某道题的答案决定是否显示当前题目
    condition: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # 语义标记: 把某道普通题标记为系统字段, 提交时其答案被抽取到 Submission 的结构化列。
    # player_name=玩家名(白名单关联键), qq=联系QQ, NULL=普通题。这样新增联系字段只需出题+打标, 不改代码。
    role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # 关系
    survey: Mapped["Survey"] = relationship("Survey", back_populates="questions")
    answers: Mapped[list["Answer"]] = relationship("Answer", back_populates="question", cascade="all, delete-orphan")


class Submission(Base):
    """问卷提交"""
    __tablename__ = "submissions"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False)
    
    # 提交者信息 (player_name 可空: 仅白名单卷/配了 role=player_name 题时才必填, 匿名收集表可空)
    player_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)  # 玩家名
    qq: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # 联系QQ (从 role=qq 题抽取, 可空)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)  # IP地址
    
    # 时间记录
    fill_duration: Mapped[Optional[float]] = mapped_column(nullable=True)  # 填写耗时（秒）
    first_viewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 管理界面首次查看时间
    
    # 审核状态
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, approved, rejected
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 审核时间
    reviewed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 审核者 admin_id
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 审核备注
    
    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)  # 提交时间

    # 自助查询/领码凭据: 提交时生成的不可枚举随机串 (secrets.token_urlsafe(32) -> 43 字符)。
    # 玩家凭此 token (而非可枚举的自增 id 或明文玩家名) 查询审核进度, 并在通过后自助领取注册码。
    # nullable: 本列引入前的历史提交无 token, 不可自助 (符合预期)。
    token: Mapped[Optional[str]] = mapped_column(String(43), unique=True, index=True, nullable=True)
    # 注册码领取时间: 非空即"已领取"。每个提交仅放码一次, 防重放/重复签发; null=未领取。
    code_issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 该 QQ 提交时是否在审核群: True=在, False=不在(面板标记"未在审核群"), None=未知/未检查。
    # 由 bot 处理 submit 通知后回填 (get_group_member_info 结果)。
    in_review_group: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # 关系
    survey: Mapped["Survey"] = relationship("Survey", back_populates="submissions")
    answers: Mapped[list["Answer"]] = relationship("Answer", back_populates="submission", cascade="all, delete-orphan")


class Answer(Base):
    """回答"""
    __tablename__ = "answers"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), nullable=False)
    
    # 回答内容 (JSON)
    # 单选: {"value": "A"}
    # 多选: {"values": ["A", "B"]}
    # 判断: {"value": true}
    # 文本: {"text": "..."}
    # 图片: {"images": ["upload/xxx.jpg", ...]}
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    
    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    
    # 关系
    submission: Mapped["Submission"] = relationship("Submission", back_populates="answers")
    question: Mapped["Question"] = relationship("Question", back_populates="answers")


class UploadedFile(Base):
    """上传的文件"""
    __tablename__ = "uploaded_files"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    
    # 文件信息
    filename: Mapped[str] = mapped_column(String(255), nullable=False)  # 原始文件名
    stored_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)  # 存储文件名
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)  # 存储路径
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)  # 文件大小 (bytes)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)  # MIME 类型
    
    # 关联
    submission_id: Mapped[Optional[int]] = mapped_column(ForeignKey("submissions.id"), nullable=True)
    
    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ActivityLog(Base):
    """活动日志"""
    __tablename__ = "activity_logs"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    
    # 活动类型: submit (提交问卷), approved (审核通过), rejected (审核拒绝)
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    
    # 目标玩家名
    player_name: Mapped[str] = mapped_column(String(64), nullable=False)
    
    # 操作者（审核人用户名，提交时为空）
    operator: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    
    # 关联的提交 ID
    submission_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    
    # 备注
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class BotNotification(Base):
    """待 QQ 机器人发送的审核群通知队列 (插件轮询消费)。

    提交/审核时入队一条; NapCat 插件轮询 /internal/notifications 取 pending,
    在审核群 @ 对应 QQ 后回调 ack 置 done。submit 类型的 ack 会回填 Submission.in_review_group。
    """
    __tablename__ = "bot_notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False)
    qq: Mapped[str] = mapped_column(String(20), nullable=False)  # 通知目标 QQ (问卷填写的, 已校验纯数字)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # submit / approved / rejected
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # rejected 时的审核备注
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending / done
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
