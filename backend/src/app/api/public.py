from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas import ApiResponse, SubmissionCreate, PublicSurveyResponse
from app.services import SurveyService, SubmissionService, FileService, ActivityService
from app.services import bot_notify
from app.services.survey import is_question_visible
from app.services.mod_client import issue_registration_code as mod_issue_registration_code
from app.core import (
    verify_turnstile,
    check_ip_rate_limit,
    record_ip_submission,
    check_upload_rate_limit,
    record_ip_upload,
    check_regcode_rate_limit,
    record_regcode_attempt,
    check_query_rate_limit,
    check_submit_time,
    get_real_ip,
    get_security_config,
)


router = APIRouter(prefix="/public", tags=["公开接口"])


@router.get("/security-config", response_model=ApiResponse)
async def get_security_settings():
    """获取安全配置（供前端使用）"""
    return ApiResponse(
        success=True,
        data=get_security_config()
    )



@router.get("/survey/active", response_model=ApiResponse)
async def get_active_survey(
    db: AsyncSession = Depends(get_db),
):
    """获取当前激活的问卷（公开，无需认证）"""
    survey = await SurveyService.get_active_survey(db)
    
    if not survey:
        raise HTTPException(status_code=404, detail="当前没有可用的问卷")
    
    # 获取问题（可能是随机的）
    questions = await SubmissionService.get_random_questions(survey)
    
    return ApiResponse(
        success=True,
        data={
            "code": survey.code,
            "title": survey.title,
            "description": survey.description,
            "questions": [
                {
                    "id": q.id,
                    "title": q.title,
                    "description": q.description,
                    "type": q.type,
                    "options": q.options,
                    "is_required": q.is_required,
                    "validation": q.validation,
                    "condition": q.condition,
                    "role": q.role,
                }
                for q in questions
            ],
        }
    )


@router.get("/surveys", response_model=ApiResponse)
async def list_public_surveys(
    category: str | None = Query(None, description="按栏目过滤: whitelist / collection"),
    db: AsyncSession = Depends(get_db),
):
    """门户可选问卷列表（公开，无需认证）。

    返回 启用+已发布+公开可见 的卷的轻量摘要（不含题目明细、不泄露提交量）,
    已按 置顶 > 排序位 > 创建时间 排好序。取代旧的'仅取单个激活卷'。
    """
    surveys = await SurveyService.list_public_surveys(db, category=category)
    return ApiResponse(
        success=True,
        data={
            "surveys": [
                {
                    "code": s.code,
                    "title": s.title,
                    "description": s.description,
                    "summary": s.summary,
                    "category": s.category,
                    "cover_url": s.cover_url,
                    "icon": s.icon,
                    "theme_color": s.theme_color,
                    "estimated_minutes": s.estimated_minutes,
                    "is_pinned": s.is_pinned,
                    "sort_order": s.sort_order,
                    "question_count": len(s.questions),
                }
                for s in surveys
            ]
        },
    )


@router.get("/surveys/{code}", response_model=ApiResponse)
async def get_public_survey(
    code: str,
    db: AsyncSession = Depends(get_db),
):
    """获取问卷（公开，无需认证）"""
    survey = await SurveyService.get_survey_by_code(db, code)
    
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")
    
    if not survey.is_active:
        raise HTTPException(status_code=400, detail="问卷已关闭")
    
    # 获取问题（可能是随机的）
    questions = await SubmissionService.get_random_questions(survey)
    
    return ApiResponse(
        success=True,
        data={
            "code": survey.code,
            "title": survey.title,
            "description": survey.description,
            "questions": [
                {
                    "id": q.id,
                    "title": q.title,
                    "description": q.description,
                    "type": q.type,
                    "options": q.options,
                    "is_required": q.is_required,
                    "validation": q.validation,
                    "condition": q.condition,
                    "role": q.role,
                }
                for q in questions
            ],
        }
    )


@router.post("/surveys/{code}/submit", response_model=ApiResponse)
async def submit_survey(
    code: str,
    data: SubmissionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """提交问卷（公开，无需认证）"""
    survey = await SurveyService.get_survey_by_code(db, code)
    
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")
    
    if not survey.is_active:
        raise HTTPException(status_code=400, detail="问卷已关闭")
    
    # 获取真实 IP 地址
    ip_address = get_real_ip(request)
    
    # === 安全检查 ===
    
    # 1. Turnstile 验证
    await verify_turnstile(data.turnstile_token, ip_address)
    
    # 2. IP 频率限制检查
    await check_ip_rate_limit(ip_address, code)
    
    # 3. 提交时间检测，同时获取填写耗时
    fill_duration = check_submit_time(data.start_time)
    
    # === 业务逻辑验证 ===
    
    # 添加调试日志
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"[Submit] 玩家: {data.player_name}, 答案数: {len(data.answers)}")
    logger.info(f"[Submit] 问卷问题 IDs: {[q.id for q in survey.questions]}")
    logger.info(f"[Submit] 提交答案 IDs: {[a.question_id for a in data.answers]}")
    
    # 验证答案
    question_ids = {q.id for q in survey.questions}
    for answer in data.answers:
        if answer.question_id not in question_ids:
            logger.warning(f"[Submit] 无效问题 ID: {answer.question_id}, 有效 IDs: {question_ids}")
            raise HTTPException(
                status_code=400, 
                detail=f"无效的问题 ID: {answer.question_id}"
            )
    
    # 构建答案映射，用于检查条件题的依赖
    answer_map = {a.question_id: a.content for a in data.answers}
    question_map = {q.id: q for q in survey.questions}

    # 检查必填问题（考虑条件题逻辑）
    # 对于随机问卷，前端只收到部分题目，无法在后端验证完整性
    if not survey.is_random:
        # 只检查可见的必填题 (condition.depends_on 语义=依赖题 question_id)
        required_questions = {
            q.id for q in survey.questions
            if q.is_required and is_question_visible(q.condition, answer_map, question_map)
        }
        answered_questions = {a.question_id for a in data.answers}
        missing = required_questions - answered_questions
        if missing:
            missing_titles = [question_map[qid].title for qid in missing if qid in question_map]
            logger.warning(f"[Submit] 缺少必填问题: {missing}, 标题: {missing_titles}")
            raise HTTPException(
                status_code=400,
                detail=f"缺少必填问题的答案: {missing_titles}"
            )
    else:
        # 对于随机问卷，只验证用户回答的必填题是否都有内容
        for answer in data.answers:
            question = question_map.get(answer.question_id)
            if question and question.is_required:
                # 检查必填题是否有有效内容
                content = answer.content
                logger.info(f"[Submit] 检查必填题 {question.id}: content={content}")
                if not content:
                    logger.warning(f"[Submit] 必填问题未作答: {question.title}")
                    raise HTTPException(
                        status_code=400,
                        detail=f"必填问题未作答: {question.title}"
                    )
    
    # === 按题目 role 标记抽取系统字段 (玩家名 / QQ) ===
    def _role_scalar(content: dict):
        if not content:
            return None
        for key in ("text", "value"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    role_player_name = None
    role_qq = None
    for q in survey.questions:
        q_role = getattr(q, "role", None)
        if q_role == "player_name" and q.id in answer_map:
            role_player_name = _role_scalar(answer_map[q.id])
        elif q_role == "qq" and q.id in answer_map:
            role_qq = _role_scalar(answer_map[q.id])

    # 玩家名优先用题目标记抽取, 兼容旧前端顶层 player_name; 缺失则拒绝 (白名单关联键不能空)
    effective_player_name = (role_player_name or data.player_name or "").strip()
    if not effective_player_name:
        raise HTTPException(
            status_code=400,
            detail="缺少玩家名: 请填写玩家名 (或在问卷中配置一道标记为玩家名的题)"
        )

    # QQ 若填写须为纯数字且长度合法 (机器人按 QQ @ 通知/主群准入; 上限 15 宽于真实 QQ、严于列宽 String(20))
    if role_qq and (not role_qq.isdigit() or len(role_qq) > 15):
        raise HTTPException(status_code=400, detail="QQ号需为纯数字且长度合法")

    # 创建提交（包含填写耗时 + 按 role 抽取的玩家名/QQ）
    submission = await SubmissionService.create_submission(
        db, survey, data, ip_address, fill_duration,
        player_name=effective_player_name, qq=role_qq,
    )

    # 记录活动日志
    await ActivityService.log_submit(db, effective_player_name, submission.id)

    # 入队审核群通知 (尽力而为: 入队失败不影响提交本身)
    try:
        await bot_notify.enqueue(db, submission, bot_notify.SUBMIT)
    except Exception:
        await db.rollback()  # 清掉入队失败的脏会话, 不污染后续 (尽力而为, 不影响提交)
        logger.warning("入队 submit 通知失败 (不影响提交)", exc_info=True)

    # 记录 IP 提交（用于频率限制）
    await record_ip_submission(ip_address, code)
    
    return ApiResponse(
        success=True,
        data={
            "id": submission.id,
            # 自助凭据: 玩家需妥善保存, 凭此查询进度并在通过后领取注册码
            "token": submission.token,
            "message": "提交成功，请等待审核",
        }
    )


@router.post("/upload", response_model=ApiResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """上传文件（公开，用于问卷中的图片上传）"""
    # 获取真实 IP 地址并检查上传频率
    ip_address = get_real_ip(request)
    await check_upload_rate_limit(ip_address)
    
    uploaded = await FileService.save_file(db, file)
    
    # 记录上传
    await record_ip_upload(ip_address)
    
    return ApiResponse(
        success=True,
        data={
            "filename": uploaded.filename,
            "stored_name": uploaded.stored_name,
            "url": FileService.get_file_url(uploaded.stored_name),
            "size": uploaded.file_size,
            "mime_type": uploaded.mime_type,
        }
    )


def _submission_status_dict(sub) -> dict:
    """把一条提交序列化为玩家可见的状态字典 (查询进度用)。明文码不在此出现。"""
    status_text = {
        "pending": "待审核",
        "approved": "已通过",
        "rejected": "未通过",
    }.get(sub.status, "未知")
    code_issued = sub.code_issued_at is not None
    return {
        "id": sub.id,
        "token": sub.token,
        "player_name": sub.player_name,
        "status": sub.status,
        "status_text": status_text,
        # 时间线
        "timeline": {
            "submitted_at": sub.created_at.isoformat() if sub.created_at else None,
            "first_viewed_at": sub.first_viewed_at.isoformat() if sub.first_viewed_at else None,
            "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
        },
        # 填写耗时（格式化为分:秒）
        "fill_duration": _format_duration(sub.fill_duration) if sub.fill_duration else None,
        # 审核备注（仅在被拒绝时显示）
        "review_note": sub.review_note if sub.status == "rejected" else None,
        # 问卷标题
        "survey_title": sub.survey.title if sub.survey else None,
        # 领码状态: 已领取 / 可领取 (通过且未领)
        "code_issued": code_issued,
        "can_get_code": sub.status == "approved" and not code_issued,
    }


@router.get("/submissions/query", response_model=ApiResponse)
async def query_submission_status(
    request: Request,
    token: str = Query(..., min_length=20, max_length=64, description="提交凭据 (提交成功后获得)"),
    db: AsyncSession = Depends(get_db),
):
    """
    凭 token 查询单条提交的审核进度（公开接口）。

    取代旧的按明文玩家名/QQ 查询: token 不可枚举且仅提交者本人持有,
    杜绝任何人凭他人玩家名探测其审核状态。返回审核进度时间线与领码状态。
    """
    # 独立 IP 限流 (per-minute): token 不可枚举无需防爆破, 仅防随机 token 狂刷
    await check_query_rate_limit(get_real_ip(request))

    submission = await SubmissionService.get_submission_by_token(db, token)
    if not submission:
        raise HTTPException(status_code=404, detail="凭据无效或未找到对应的问卷提交")

    return ApiResponse(
        success=True,
        data={"submission": _submission_status_dict(submission)},
    )


@router.post("/submissions/{token}/registration-code", response_model=ApiResponse)
async def redeem_registration_code(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    凭 token 自助领取注册码（公开接口, 独立 IP 限流）。

    仅当提交已审核通过且未领取过才放码: 调 mod 为该玩家名签发一次性注册码并标记已领取。
    每个提交仅放码一次; 未过审 / 已领取分别返回明确状态, 过期由玩家联系管理员补发。
    """
    ip_address = get_real_ip(request)
    await check_regcode_rate_limit(ip_address)
    await record_regcode_attempt(ip_address)

    submission = await SubmissionService.get_submission_by_token(db, token)
    if not submission:
        raise HTTPException(status_code=404, detail="凭据无效或未找到对应的问卷提交")

    status, code_data = await SubmissionService.issue_registration_code(
        db, submission, mod_issue_registration_code
    )

    if status == "not_approved":
        raise HTTPException(status_code=409, detail="问卷尚未通过审核, 暂不能领取注册码")

    if status == "already_issued":
        return ApiResponse(
            success=True,
            data={
                "already_issued": True,
                "message": "该问卷的注册码已领取过。如遗失或已过期, 请联系管理员补发。",
            },
        )

    # status == "ok": 明文码仅在此响应出现, 严禁写日志
    return ApiResponse(
        success=True,
        data={
            "registration_code": code_data["registration_code"],
            "code_expires_minutes": code_data.get("code_expires_minutes"),
            "message": "注册码已生成, 请在游戏内使用 /register 完成注册。",
        },
    )


def _format_duration(seconds: float) -> str:
    """格式化填写耗时"""
    if seconds < 60:
        return f"{int(seconds)}秒"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}分{secs}秒"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    return f"{hours}小时{mins}分"
