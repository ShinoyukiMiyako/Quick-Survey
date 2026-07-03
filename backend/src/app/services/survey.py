import secrets
import random
from typing import Optional
from datetime import datetime, timezone
from sqlalchemy import select, func, delete, String, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Survey, Question, Submission, Answer
from app.schemas import (
    SurveyCreate, SurveyUpdate, QuestionCreate, QuestionUpdate,
    SubmissionCreate, SubmissionReview
)


def is_question_visible(condition, answer_map: dict, question_map: dict) -> bool:
    """依据条件逻辑判断题目是否应对用户可见 (纯函数, 便于单测)。

    condition.depends_on 语义: 依赖题的 question_id (稳定引用, 不随题序/编辑变化)。
    历史上曾按"题目 ID 升序的位置索引"解释, 现统一为 question_id, 与前端及
    模型注释对齐; 存量数据由迁移脚本一次性重映射。

    show_when 与依赖题答案的比较规则:
    - single/boolean: 答案存放在 content["value"]
    - multiple:       答案存放在 content["values"] (list), 命中任一即触发
    - text:           答案存放在 content["text"]
    - image:          不参与条件比较

    answer_map:  {question_id: content}
    question_map: {question_id: question} (仅用于判定依赖题是否存在于本卷)
    """
    if not condition:
        return True

    depends_on = condition.get("depends_on")
    show_when = condition.get("show_when")

    if depends_on is None or show_when is None:
        return True

    # 依赖题不存在 (已删 / 随机卷未抽中) 时不因条件隐藏, 交由必填等其它规则处理
    if depends_on not in question_map:
        return True

    depend_answer = answer_map.get(depends_on)
    if not depend_answer:
        return False  # 依赖的题目没有回答, 条件题不可见

    # 兼容不同题型的答案字段
    if "value" in depend_answer and depend_answer["value"] not in (None, ""):
        answer_value = depend_answer["value"]
    elif "values" in depend_answer and depend_answer["values"]:
        answer_value = depend_answer["values"]
    elif "text" in depend_answer and depend_answer["text"]:
        answer_value = depend_answer["text"]
    else:
        return False

    # 标准化 show_when 为集合
    if isinstance(show_when, list):
        show_set = {str(v) for v in show_when}
    else:
        show_set = {str(show_when)}

    # multiple 题型: answer_value 是列表, 命中任一值即触发
    if isinstance(answer_value, list):
        return any(str(v) in show_set for v in answer_value)
    return str(answer_value) in show_set


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
        survey = Survey(
            title=data.title,
            description=data.description,
            code=code,
            is_random=data.is_random,
            random_count=data.random_count,
            category=data.category,
            visibility=data.visibility,
            cover_url=data.cover_url,
            icon=data.icon,
            theme_color=data.theme_color,
            summary=data.summary,
            estimated_minutes=data.estimated_minutes,
            created_by=created_by,
        )
        
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
                condition=q_data.condition.model_dump() if q_data.condition else None,
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
            condition=data.condition.model_dump() if data.condition else None,
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
            update_data["condition"] = data.condition.model_dump()
        
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
            status="pending",
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
    ) -> tuple[list[Submission], int]:
        """获取提交列表"""
        query = select(Submission).options(selectinload(Submission.survey))
        count_query = select(func.count(Submission.id))
        
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
