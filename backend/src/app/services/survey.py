import hashlib
import hmac
import secrets
import random
from copy import deepcopy
from typing import Optional
from datetime import datetime, timezone
from sqlalchemy import select, func, delete, String, and_, inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from app.core.timefmt import iso_utc
from app.models import Survey, Question, Submission, Answer
from app.schemas import (
    SurveyCreate, SurveyUpdate, QuestionCreate, QuestionUpdate,
    SubmissionCreate, SubmissionReview
)
from app.services.conditions import is_question_visible  # noqa: F401 供既有导入
from app.services.question_types import answer_to_cell, is_answerable


# 访问口令哈希算法标识与轮数: 现有依赖里没有 passlib/bcrypt, 为一个可选的问卷口令
# 引入新依赖不划算, 用 stdlib 的 pbkdf2 即可满足"不可逆 + 加盐 + 拖慢暴力破解"。
_PASSWORD_ALGO = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 200_000


def hash_access_password(raw: str) -> str:
    """把访问口令哈希成入库串: "pbkdf2_sha256$<轮数>$<盐hex>$<摘要hex>"。

    轮数写进串里, 日后调高轮数时旧口令仍能按自己的轮数验通过, 不需要强制所有卷重设口令。
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return f"{_PASSWORD_ALGO}${_PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_access_password(raw: Optional[str], stored: Optional[str]) -> bool:
    """校验访问口令。stored 为空表示该卷没设口令, 一律放行。"""
    if not stored:
        return True

    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != _PASSWORD_ALGO:
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
    except ValueError:
        # 库里的哈希串被写坏时按"验不过"处理: 口令闸门必须 fail-closed, 不能因解析失败反而放行
        return False
    if iterations <= 0:
        return False

    digest = hashlib.pbkdf2_hmac("sha256", (raw or "").encode("utf-8"), salt, iterations)
    # 用 compare_digest 而非 ==: 常数时间比较, 不给攻击者按前缀逐字节猜口令的时序侧信道
    return hmac.compare_digest(digest.hex(), parts[3])


async def hash_access_password_async(raw: str) -> str:
    """hash_access_password 的 await 版本, 结果与同步版等价, 只是把 pbkdf2 推到线程池跑。

    async 端点里一律走本版本: 20 万轮 pbkdf2 单次要 60-100ms, 在事件循环线程上同步跑
    等于把整个进程的 API 停摆这么久 —— 卡住的不只是发起者, 是同一时刻所有在飞的请求。
    同步版只留给纯函数场景 (单测、非 async 上下文)。
    """
    return await run_in_threadpool(hash_access_password, raw)


async def verify_access_password_async(raw: Optional[str], stored: Optional[str]) -> bool:
    """verify_access_password 的 await 版本, 结果与同步版等价, 只是把 pbkdf2 推到线程池跑。

    口令校验挂在 unlock 与 submit 两条**未认证**的公开路径上, 谁都能无限次触发。
    同步跑就等于给了一个零成本的拒绝服务开关: 循环打口令校验即可让整个 API 排队。
    公开端点必须用本版本, 让 CPU 活落在线程池、事件循环继续收发别的请求。
    """
    return await run_in_threadpool(verify_access_password, raw, stored)


def _as_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """把可能带时区的时间统一成 naive UTC。

    SQLite 取回的时间一律是 naive, 而 pydantic 从前端 ISO 串解析出来的可能带 tz,
    两者直接比较会抛 "can't compare offset-naive and offset-aware datetimes"。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


# 各不可填状态的默认提示; 卷上配了 closed_message 时以运营文案优先。
AVAILABILITY_MESSAGES: dict[str, str] = {
    "inactive": "该问卷已停用",
    "unpublished": "该问卷尚未发布",
    "not_started": "该问卷尚未开始, 请稍后再来",
    "ended": "该问卷已截止",
    "full": "该问卷提交名额已满",
}


def compute_availability(survey, submission_count: int, now: datetime) -> tuple[str, Optional[str]]:
    """判定问卷当前是否可填, 返回 (state, message)。纯函数, 便于单测。

    优先级自上而下: 停用 > 未发布 > 未开始 > 已截止 > 名额已满 > 开放 ——
    管理员手动停用是最强意图, 排在时间窗与名额之前, 免得截止的卷被报成"已截止"而掩盖"已停用"。
    """
    now = _as_naive_utc(now)
    starts_at = _as_naive_utc(survey.starts_at)
    ends_at = _as_naive_utc(survey.ends_at)

    if not survey.is_active:
        state = "inactive"
    elif survey.status != "published":
        state = "unpublished"
    elif starts_at is not None and starts_at > now:
        state = "not_started"
    elif ends_at is not None and ends_at <= now:
        state = "ended"
    elif survey.max_submissions is not None and submission_count >= survey.max_submissions:
        state = "full"
    else:
        return "open", None

    return state, (survey.closed_message or AVAILABILITY_MESSAGES[state])


def _answer_to_cell(content: Optional[dict], qtype: str) -> str:
    """把一条答案 content 按题型压成 CSV 单元格文本。

    实现已下沉到 question_types.answer_to_cell; 本函数保留原名原参数序作为既有调用方的入口。
    """
    return answer_to_cell(qtype, content, None)


def build_submissions_csv(survey, submissions) -> str:
    """把某问卷的全部提交导出为 CSV 文本 (含 Excel BOM; 每题一列, 系统字段在前)。纯函数, 便于单测。"""
    import csv
    import io

    # 分节说明块不收答案, 列进来只会多出一整列空白
    questions = [q for q in sorted(survey.questions, key=lambda q: q.order) if is_answerable(q.type)]
    headers = ["提交ID", "提交时间", "玩家名", "QQ", "状态"] + [q.title for q in questions]

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM, 让 Excel 正确识别中文
    writer = csv.writer(buf)
    writer.writerow(headers)
    for sub in submissions:
        amap = {a.question_id: a.content for a in sub.answers}
        row = [
            sub.id,
            iso_utc(sub.created_at) or "",
            sub.player_name or "",
            sub.qq or "",
            sub.status,
        ]
        for q in questions:
            # 传 options 让选项题导出人看得懂的 label 而不是裸 value;
            # getattr 兜底是因为本函数按鸭子类型接受任意带题目字段的对象 (既有单测传的是轻量 stub)
            row.append(answer_to_cell(q.type, amap.get(q.id), getattr(q, "options", None)))
        writer.writerow(row)
    return buf.getvalue()


# SurveyCreate 与 Survey 同名同义、可直接搬运的字段名单。
# 逐字段手写 Survey(...) 在设置项变多后必然漏, 漏一个就是"面板创建时填了却没保存",
# 故集中成名单批量搬运。不在此列的: code(现生成)、questions(单独建题)、
# is_active/status(创建即启用发布, 走列默认)。四个动作开关在列内 ——
# 未显式提供时由下面按 category 播种, 显式提供时以面板意图为准。
_SURVEY_CREATE_PASSTHROUGH = frozenset({
    "title", "description", "is_random", "random_count",
    "category", "visibility", "status",
    "cover_url", "icon", "theme_color", "summary", "estimated_minutes",
    "starts_at", "ends_at", "max_submissions", "max_submissions_per_ip",
    "require_consent", "privacy_notice", "closed_message", "success_message",
    "review_required", "action_add_whitelist", "action_issue_code",
    "action_notify_group", "action_webhook", "webhook_url", "notify_group_id",
})

# 复制问卷时不沿用原卷的列: 身份(自己生成)、生命周期(副本一律停用草稿)、审计(记副本自己的)。
# 用"排除法 + 遍历 mapper 列"而非正向名单, 这样 surveys 表以后加列会自动跟着复制, 不会漏。
_SURVEY_COPY_SKIP = frozenset({
    "id", "code", "title",
    "is_active", "status",
    "created_at", "updated_at", "created_by",
})

# 复制题目时不沿用的列: 主键与归属由副本自己决定, created_at 记副本的创建时间。
_QUESTION_COPY_SKIP = frozenset({"id", "survey_id", "created_at"})


def _remap_condition(condition: Optional[dict], id_map: dict[int, int]) -> Optional[dict]:
    """把条件里引用的 question_id 从原题 id 换成副本的新题 id。

    映射不到的规则直接丢弃: 留着就是指向别的卷的悬空引用, 副本的分支会被原卷的答案左右。
    新旧两种条件形态都要处理 —— 存量库里全是旧形态 {depends_on, show_when}。
    """
    if not condition:
        return None

    rules = condition.get("rules")
    if isinstance(rules, list):
        new_rules = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            new_id = id_map.get(rule.get("question_id"))
            if new_id is None:
                continue
            new_rules.append({**rule, "question_id": new_id})
        # 规则全丢光等于没有条件, 置空避免留下一个永远不成立的空壳规则组
        return {**condition, "rules": new_rules} if new_rules else None

    new_id = id_map.get(condition.get("depends_on"))
    if new_id is None:
        return None
    return {**condition, "depends_on": new_id}


class SurveyService:
    """问卷服务"""

    @staticmethod
    async def create_survey(
        db: AsyncSession,
        data: SurveyCreate,
        created_by: Optional[int] = None
    ) -> Survey:
        """创建问卷"""
        # 使用问卷标题生成简单的访问码
        code = secrets.token_urlsafe(8)[:8]
        # 按栏目播种提交后动作默认: 收集表纯收集(免审+零白名单动作), 白名单卷全开
        wl = data.category != "collection"
        survey = Survey(
            code=code,
            review_required=wl,
            action_add_whitelist=wl,
            action_issue_code=wl,
            action_notify_group=wl,
            created_by=created_by,
        )

        # exclude_unset: 只落库面板真的填了的字段, 其余交给列默认值, 免得 schema 默认与列默认打架
        payload = data.model_dump(include=set(_SURVEY_CREATE_PASSTHROUGH), exclude_unset=True)
        for field in ("starts_at", "ends_at"):
            # 列里统一存 naive UTC: 带 tz 的时间进 SQLite 会被丢掉时区信息, 变成错的墙钟时间
            if field in payload:
                payload[field] = _as_naive_utc(payload[field])
        for field, value in payload.items():
            setattr(survey, field, value)

        # 添加问题
        for i, q_data in enumerate(data.questions):
            question = Question(
                title=q_data.title,
                description=q_data.description,
                type=q_data.type,
                options=[opt.model_dump() for opt in q_data.options] if q_data.options else None,
                is_required=q_data.is_required,
                is_pinned=q_data.is_pinned,
                order=q_data.order if q_data.order else i,
                validation=q_data.validation.model_dump() if q_data.validation else None,
                # exclude_none: 条件有新旧两套字段, 不排除 None 会把另一套的空字段一起写进 JSON
                condition=q_data.condition.model_dump(exclude_none=True) if q_data.condition else None,
                role=q_data.role,
            )
            survey.questions.append(question)

        db.add(survey)
        await db.commit()
        await db.refresh(survey)
        return survey
    
    @staticmethod
    async def get_survey_by_id(db: AsyncSession, survey_id: int) -> Optional[Survey]:
        """通过 ID 获取问卷"""
        result = await db.execute(
            select(Survey)
            .options(selectinload(Survey.questions))
            .where(Survey.id == survey_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_survey_by_code(db: AsyncSession, code: str) -> Optional[Survey]:
        """通过访问码获取问卷"""
        result = await db.execute(
            select(Survey)
            .options(selectinload(Survey.questions))
            .where(Survey.code == code)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_active_survey(db: AsyncSession) -> Optional[Survey]:
        """获取当前激活的问卷（返回第一个激活的问卷）"""
        result = await db.execute(
            select(Survey)
            .options(selectinload(Survey.questions))
            .where(Survey.is_active == True)
            .order_by(Survey.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_surveys(
        db: AsyncSession,
        page: int = 1,
        size: int = 20,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
        category: Optional[str] = None,
    ) -> tuple[list[Survey], int]:
        """获取问卷列表 (管理端), 按 置顶 > 排序位 > 创建时间, 与门户展示顺序一致。"""
        query = select(Survey)
        count_query = select(func.count(Survey.id))

        if search:
            query = query.where(Survey.title.contains(search))
            count_query = count_query.where(Survey.title.contains(search))

        if is_active is not None:
            query = query.where(Survey.is_active == is_active)
            count_query = count_query.where(Survey.is_active == is_active)

        if category is not None:
            query = query.where(Survey.category == category)
            count_query = count_query.where(Survey.category == category)

        # 获取总数
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页 (置顶优先, 再按排序位, 最后按创建时间)
        query = query.order_by(
            Survey.is_pinned.desc(), Survey.sort_order.asc(), Survey.created_at.desc()
        )
        query = query.offset((page - 1) * size).limit(size)

        result = await db.execute(query)
        surveys = result.scalars().all()

        return list(surveys), total

    @staticmethod
    async def list_public_surveys(
        db: AsyncSession, category: Optional[str] = None
    ) -> list[Survey]:
        """门户可选问卷列表: 启用 + 已发布 + 公开可见, 按 置顶 > 排序位 > 创建时间。

        取代'仅取最新一个激活卷'的单卷假设, 支撑多表单入口。
        """
        query = (
            select(Survey)
            .options(selectinload(Survey.questions))
            .where(
                Survey.is_active == True,
                Survey.status == "published",
                Survey.visibility == "public",
            )
        )
        if category is not None:
            query = query.where(Survey.category == category)
        query = query.order_by(
            Survey.is_pinned.desc(), Survey.sort_order.asc(), Survey.created_at.desc()
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def reorder_surveys(db: AsyncSession, orders: list) -> int:
        """批量更新展示排序位 (orders: [SurveyReorderItem(id, sort_order)])。返回更新条数。"""
        updated = 0
        for item in orders:
            result = await db.execute(select(Survey).where(Survey.id == item.id))
            survey = result.scalar_one_or_none()
            if survey is None:
                continue
            survey.sort_order = item.sort_order
            updated += 1
        if updated:
            await db.commit()
        return updated
    
    @staticmethod
    async def get_survey_stats(db: AsyncSession) -> dict:
        """获取问卷统计"""
        # 启用中的问卷数
        active_result = await db.execute(
            select(func.count(Survey.id)).where(Survey.is_active == True)
        )
        active = active_result.scalar() or 0
        
        # 已停用的问卷数
        inactive_result = await db.execute(
            select(func.count(Survey.id)).where(Survey.is_active == False)
        )
        inactive = inactive_result.scalar() or 0
        
        return {
            "active": active,
            "inactive": inactive,
            "total": active + inactive,
        }
    
    @staticmethod
    async def update_survey(
        db: AsyncSession, 
        survey: Survey, 
        data: SurveyUpdate
    ) -> Survey:
        """更新问卷"""
        update_data = data.model_dump(exclude_unset=True)

        # access_password 是写入型的非列字段: 明文永远不落库, 也不能 setattr 到 Survey 上。
        # 非空串=设为新口令; 显式传空串/None=清除口令; 整个键不传=保持原样(面板不必回填口令)。
        if "access_password" in update_data:
            raw = update_data.pop("access_password")
            survey.access_password_hash = hash_access_password(raw) if raw else None

        for field in ("starts_at", "ends_at"):
            # 列里统一存 naive UTC: 带 tz 的时间进 SQLite 会被丢掉时区信息, 变成错的墙钟时间
            if field in update_data:
                update_data[field] = _as_naive_utc(update_data[field])

        for field, value in update_data.items():
            setattr(survey, field, value)

        survey.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(survey)
        return survey
    
    @staticmethod
    async def delete_survey(db: AsyncSession, survey: Survey) -> None:
        """删除问卷"""
        await db.delete(survey)
        await db.commit()
    
    @staticmethod
    async def get_question_count(db: AsyncSession, survey_id: int) -> int:
        """获取问卷的问题数量"""
        result = await db.execute(
            select(func.count(Question.id)).where(Question.survey_id == survey_id)
        )
        return result.scalar() or 0
    
    @staticmethod
    async def get_submission_count(db: AsyncSession, survey_id: int) -> int:
        """获取问卷的提交数量"""
        result = await db.execute(
            select(func.count(Submission.id)).where(Submission.survey_id == survey_id)
        )
        return result.scalar() or 0

    @staticmethod
    async def count_submissions_by_ip(db: AsyncSession, survey_id: int, ip: Optional[str]) -> int:
        """统计某 IP 在该卷的提交数 (每 IP 提交上限闸门)。

        ip 为空 = 取不到真实来源 IP, 返回 0 表示不限: 宁可放行也不能因取不到 IP 把所有人挡在门外。
        """
        if not ip:
            return 0
        result = await db.execute(
            select(func.count(Submission.id)).where(
                and_(Submission.survey_id == survey_id, Submission.ip_address == ip)
            )
        )
        return result.scalar() or 0

    @staticmethod
    async def duplicate_survey(
        db: AsyncSession,
        survey_id: int,
        created_by: Optional[int] = None,
    ) -> Survey:
        """深拷贝问卷与全部题目 (不复制提交数据), 副本落成未启用的草稿。

        两段式: 先把题目落库拿到新 id, 再按 旧题id->新题id 重写 condition 里的 question_id。
        不重写的话副本的分支逻辑会指向原卷的题, 之后删原卷题目就会把副本的条件打成悬空引用。
        """
        source = await SurveyService.get_survey_by_id(db, survey_id)
        if source is None:
            raise ValueError("问卷不存在")

        clone = Survey(
            title=f"{source.title} (副本)",
            code=secrets.token_urlsafe(8)[:8],
            # 副本一律停用草稿: 复制常用于改版, 直接上线会和原卷同时对玩家可见
            is_active=False,
            status="draft",
            created_by=created_by,
        )
        for name in sa_inspect(Survey).columns.keys():
            if name not in _SURVEY_COPY_SKIP:
                setattr(clone, name, getattr(source, name))
        db.add(clone)
        await db.flush()  # 先拿到副本的 survey_id, 题目才能挂上去

        question_fields = [
            name for name in sa_inspect(Question).columns.keys()
            if name not in _QUESTION_COPY_SKIP
        ]
        cloned_questions: dict[int, Question] = {}
        for src_q in sorted(source.questions, key=lambda q: q.order):
            new_q = Question(survey_id=clone.id)
            for name in question_fields:
                # deepcopy: options/validation/condition 是 JSON 列, 直接引用同一个 dict
                # 会让副本与原卷共享对象, 改一边串改另一边
                setattr(new_q, name, deepcopy(getattr(src_q, name)))
            db.add(new_q)
            cloned_questions[src_q.id] = new_q
        await db.flush()  # 落库拿到新题 id, 才能建立 旧题id->新题id 映射

        id_map = {old_id: new_q.id for old_id, new_q in cloned_questions.items()}
        for new_q in cloned_questions.values():
            new_q.condition = _remap_condition(new_q.condition, id_map)

        await db.commit()
        return await SurveyService.get_survey_by_id(db, clone.id)


class QuestionService:
    """问题服务"""
    
    @staticmethod
    async def add_question(
        db: AsyncSession, 
        survey_id: int, 
        data: QuestionCreate
    ) -> Question:
        """添加问题"""
        question = Question(
            survey_id=survey_id,
            title=data.title,
            description=data.description,
            type=data.type,
            options=[opt.model_dump() for opt in data.options] if data.options else None,
            is_required=data.is_required,
            is_pinned=data.is_pinned,
            order=data.order,
            validation=data.validation.model_dump() if data.validation else None,
            # exclude_none: 条件有新旧两套字段, 不排除 None 会把另一套的空字段一起写进 JSON
            condition=data.condition.model_dump(exclude_none=True) if data.condition else None,
            role=data.role,
        )
        db.add(question)
        await db.commit()
        await db.refresh(question)
        return question
    
    @staticmethod
    async def get_question_by_id(db: AsyncSession, question_id: int, load_answers: bool = False) -> Optional[Question]:
        """通过 ID 获取问题"""
        query = select(Question).where(Question.id == question_id)
        if load_answers:
            query = query.options(selectinload(Question.answers))
        result = await db.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def update_question(
        db: AsyncSession, 
        question: Question, 
        data: QuestionUpdate
    ) -> Question:
        """更新问题"""
        update_data = data.model_dump(exclude_unset=True)
        
        if "options" in update_data and update_data["options"]:
            update_data["options"] = [opt.model_dump() for opt in data.options]
        if "validation" in update_data and update_data["validation"]:
            update_data["validation"] = data.validation.model_dump()
        if "condition" in update_data and update_data["condition"]:
            # exclude_none: 条件有新旧两套字段, 不排除 None 会把另一套的空字段一起写进 JSON
            update_data["condition"] = data.condition.model_dump(exclude_none=True)

        for field, value in update_data.items():
            setattr(question, field, value)
        
        await db.commit()
        await db.refresh(question)
        return question
    
    @staticmethod
    async def delete_question(db: AsyncSession, question: Question) -> None:
        """删除问题"""
        # 先删除关联的答案（因为 SQLite 外键约束可能未启用）
        for answer in question.answers:
            await db.delete(answer)
        await db.delete(question)
        await db.commit()


class SubmissionService:
    """提交服务"""
    
    @staticmethod
    async def create_submission(
        db: AsyncSession,
        survey: Survey,
        data: SubmissionCreate,
        ip_address: Optional[str] = None,
        fill_duration: Optional[float] = None,
        player_name: Optional[str] = None,
        qq: Optional[str] = None,
    ) -> Submission:
        """创建提交。player_name/qq 由调用方 (public.submit) 按题目 role 抽取后传入。"""
        submission = Submission(
            survey_id=survey.id,
            player_name=player_name or data.player_name,
            qq=qq,
            ip_address=ip_address,
            fill_duration=fill_duration,
            # 免审卷(收集表)提交即终态 approved, 不进待审队列; 需审核卷进 pending
            status="pending" if survey.review_required else "approved",
            # 不可枚举自助凭据: 256 bit 随机, 碰撞概率可忽略。万一撞唯一索引由异常自然冒泡, 不静默吞。
            token=secrets.token_urlsafe(32),
        )
        
        # 添加答案
        for answer_data in data.answers:
            answer = Answer(
                question_id=answer_data.question_id,
                content=answer_data.content,
            )
            submission.answers.append(answer)
        
        db.add(submission)
        await db.commit()
        await db.refresh(submission)
        return submission
    
    @staticmethod
    async def get_submission_by_id(
        db: AsyncSession, 
        submission_id: int,
        mark_viewed: bool = False,
    ) -> Optional[Submission]:
        """
        通过 ID 获取提交
        
        Args:
            db: 数据库会话
            submission_id: 提交 ID
            mark_viewed: 是否标记首次查看时间
        """
        result = await db.execute(
            select(Submission)
            .options(
                selectinload(Submission.answers),
                selectinload(Submission.survey),
            )
            .where(Submission.id == submission_id)
        )
        submission = result.scalar_one_or_none()
        
        # 如果需要标记首次查看时间，且之前未查看过
        if submission and mark_viewed and submission.first_viewed_at is None:
            submission.first_viewed_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(submission)
        
        return submission
    
    @staticmethod
    async def get_submissions(
        db: AsyncSession,
        page: int = 1,
        size: int = 20,
        status: Optional[str] = None,
        survey_id: Optional[int] = None,
        player_name: Optional[str] = None,
        category: Optional[str] = None,
        review_required: Optional[bool] = None,
    ) -> tuple[list[Submission], int]:
        """获取提交列表。

        category 过滤按栏目取数 (收集表结果只看 collection);
        review_required 过滤按"这张卷是否需要人工审核"取数 —— 审核队列该用它而不是 category:
        收集表现在也能手动开人工审核, 按 category='whitelist' 筛会把这类卷的 pending 提交
        整个挡在面板外, 玩家永远停在"等待审核"。两个过滤可叠加。
        """
        query = select(Submission).options(selectinload(Submission.survey))
        count_query = select(func.count(Submission.id))

        # 两个过滤都读 surveys 列, 只联一次表: 分别 join 会被 SQLAlchemy 判成重复的 FROM 目标
        if category or review_required is not None:
            query = query.join(Survey, Submission.survey_id == Survey.id)
            count_query = count_query.join(Survey, Submission.survey_id == Survey.id)

        if category:
            query = query.where(Survey.category == category)
            count_query = count_query.where(Survey.category == category)

        if review_required is not None:
            query = query.where(Survey.review_required == review_required)
            count_query = count_query.where(Survey.review_required == review_required)

        if status:
            query = query.where(Submission.status == status)
            count_query = count_query.where(Submission.status == status)

        if survey_id:
            query = query.where(Submission.survey_id == survey_id)
            count_query = count_query.where(Submission.survey_id == survey_id)

        if player_name:
            query = query.where(Submission.player_name.contains(player_name))
            count_query = count_query.where(Submission.player_name.contains(player_name))

        # 获取总数
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0
        
        # 分页
        query = query.order_by(Submission.created_at.desc())
        query = query.offset((page - 1) * size).limit(size)
        
        result = await db.execute(query)
        submissions = result.scalars().all()

        return list(submissions), total

    @staticmethod
    async def get_submissions_with_answers(db: AsyncSession, survey_id: int) -> list[Submission]:
        """取某问卷全部提交(含答案), 供 CSV 导出。按提交时间升序。"""
        result = await db.execute(
            select(Submission)
            .options(selectinload(Submission.answers))
            .where(Submission.survey_id == survey_id)
            .order_by(Submission.created_at.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def review_submission(
        db: AsyncSession,
        submission: Submission,
        data: SubmissionReview,
        reviewed_by: int,
    ) -> Submission:
        """审核提交"""
        submission.status = data.status
        submission.review_note = data.review_note
        submission.reviewed_by = reviewed_by
        submission.reviewed_at = datetime.now(timezone.utc)
        
        await db.commit()
        await db.refresh(submission)
        return submission
    
    @staticmethod
    async def get_random_questions(survey: Survey) -> list[Question]:
        """获取随机题目（用于随机题库）
        
        随机抽题逻辑：
        1. 保留题目（is_pinned=True）始终出现
        2. 从非保留题目中随机抽取，使总数达到 random_count
        """
        questions = list(survey.questions)
        
        if survey.is_random and survey.random_count:
            # 分离保留题目和普通题目
            pinned = [q for q in questions if q.is_pinned]
            unpinned = [q for q in questions if not q.is_pinned]
            
            # 计算需要从普通题目中抽取的数量
            remaining_count = max(0, survey.random_count - len(pinned))
            remaining_count = min(remaining_count, len(unpinned))
            
            # 随机抽取普通题目
            selected_unpinned = random.sample(unpinned, remaining_count) if remaining_count > 0 else []
            
            # 合并保留题目和随机抽取的题目
            questions = pinned + selected_unpinned
        
        return sorted(questions, key=lambda q: q.order)
    
    @staticmethod
    async def get_submission_by_token(
        db: AsyncSession,
        token: str,
    ) -> Optional[Submission]:
        """
        按自助凭据 token 精确查询单条提交 (玩家查询审核进度 / 领码的统一入口)。

        取代旧的按明文玩家名/QQ 查询: 后者无凭据、可枚举他人状态。token 不可枚举,
        只有提交者本人 (或其浏览器 localStorage) 持有, 故只放行精确命中的单条。
        """
        if not token:
            return None
        result = await db.execute(
            select(Submission)
            .options(selectinload(Submission.survey))
            .where(Submission.token == token)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_approved_by_qq(db: AsyncSession, qq: str) -> Optional[Submission]:
        """按 QQ 查最近一条已通过审核的提交 (主群自动准入: 申请人 QQ 命中即放行)。无则 None。"""
        if not qq:
            return None
        result = await db.execute(
            select(Submission)
            .where(and_(Submission.qq == qq, Submission.status == "approved"))
            .order_by(Submission.reviewed_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def issue_registration_code(
        db: AsyncSession,
        submission: Submission,
        code_provider,
    ) -> tuple[str, Optional[dict]]:
        """
        领取注册码的状态机 (权威闸门集中在此, 端点只做 IP 限流 + HTTP 映射)。

        - status != approved        -> ("not_approved", None): 未过审不放码, 否则人工审核形同虚设。
        - 已领取 (code_issued_at 非空) -> ("already_issued", None): 每提交仅放码一次, 不重复向 mod 取码。
        - 否则                       -> ("ok", code_provider 返回的码数据), 并标记 code_issued_at。

        code_provider: async (player_name) -> dict (向 mod 取码, 失败时自行抛出由上层冒泡)。
        注: 先取码再标记, 取码失败不会误标"已领取"; 并发双击的罕见竞态由前端禁用按钮兜底。
        """
        if submission.status != "approved":
            return "not_approved", None
        if submission.code_issued_at is not None:
            return "already_issued", None

        code_data = await code_provider(submission.player_name)

        submission.code_issued_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(submission)
        return "ok", code_data
