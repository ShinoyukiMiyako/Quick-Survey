import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas import ApiResponse, SubmissionCreate, PublicSurveyResponse
# SurveyUnlockRequest 未列进 app.schemas 的桶导出, 从定义模块直接取, 不依赖桶文件的更新
from app.schemas.schemas import SurveyUnlockRequest
from app.services import SurveyService, SubmissionService, FileService, ActivityService
from app.services import bot_notify, webhook
from app.services.survey import (
    AVAILABILITY_MESSAGES,
    compute_availability,
    is_question_visible,
    verify_access_password_async,
)
from app.services.question_types import answer_scalar, is_answered, validate_answer
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


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["公开接口"])


def _public_question_dict(question) -> dict:
    """把一道题序列化为玩家端可见字段 (不含任何管理侧信息)。"""
    return {
        "id": question.id,
        "title": question.title,
        "description": question.description,
        "type": question.type,
        "options": question.options,
        "is_required": question.is_required,
        "validation": question.validation,
        "condition": question.condition,
        "role": question.role,
    }


async def _public_survey_payload(db: AsyncSession, survey, unlocked: bool = False) -> dict:
    """构造玩家端问卷详情响应, 是详情接口与解锁接口唯一的产出口。

    两个入口的结构必须逐字段一致 —— 前端解锁成功后直接拿返回值替换手上的问卷对象,
    结构一旦分成两份实现, 迟早有一边漏字段。unlocked=True 表示本次请求已验过口令。
    """
    submission_count = await SurveyService.get_submission_count(db, survey.id)
    state, message = compute_availability(survey, submission_count, datetime.now(timezone.utc))
    locked = survey.access_password_hash is not None and not unlocked

    # 加锁或不可填时一律不回传题目: 题干本身也是内容, 不能让人绕过口令/开放窗口把题扒走
    questions = (
        [] if locked or state != "open"
        else await SubmissionService.get_random_questions(survey)
    )

    return {
        "code": survey.code,
        "title": survey.title,
        "description": survey.description,
        # 场景标记: 前端据此决定成功页展示 (收集表不显示查询凭据/领码)
        "category": survey.category,
        "requires_review": survey.review_required,
        "issues_code": survey.action_issue_code,
        # 可填状态与口令闸门: 不可填的卷也照常返回 200, 由前端渲染状态页
        "availability": {"state": state, "message": message},
        "locked": locked,
        "require_consent": survey.require_consent,
        "privacy_notice": survey.privacy_notice,
        "theme_color": survey.theme_color,
        "icon": survey.icon,
        "success_message": survey.success_message,
        "estimated_minutes": survey.estimated_minutes,
        "questions": [_public_question_dict(q) for q in questions],
    }


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
    """获取当前激活的问卷（公开，无需认证）。

    多表单门户上线后本端点已退居兼容位 (门户走 /public/surveys)。但它必须与详情接口
    共用同一个 payload 出口: 否则口令卷/未到开放期的卷会从这条老路径被原样扒走题目,
    等于给访问控制开了后门。
    """
    survey = await SurveyService.get_active_survey(db)

    if not survey:
        raise HTTPException(status_code=404, detail="当前没有可用的问卷")

    return ApiResponse(
        success=True,
        data=await _public_survey_payload(db, survey),
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

    # 只有配了总量上限的卷才真去数提交: 没配上限时 compute_availability 根本不看这个数,
    # 逐卷无差别查一次会把门户列表打成 N+1 查询
    counts = {
        s.id: await SurveyService.get_submission_count(db, s.id)
        for s in surveys
        if s.max_submissions is not None
    }
    now = datetime.now(timezone.utc)

    items = []
    for s in surveys:
        state, message = compute_availability(s, counts.get(s.id, 0), now)
        items.append({
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
            # 不可填的卷仍然列出: 前端显示"已截止/名额已满"徽标并禁点, 比直接消失更好解释
            "availability": {"state": state, "message": message},
            "locked": s.access_password_hash is not None,
        })

    return ApiResponse(success=True, data={"surveys": items})


@router.get("/surveys/{code}", response_model=ApiResponse)
async def get_public_survey(
    code: str,
    db: AsyncSession = Depends(get_db),
):
    """获取问卷（公开，无需认证）

    停用/未开始/已截止/名额已满的卷不再报 400: 卷是存在的, 只是此刻不能填,
    统一返回 200 + availability 让前端渲染状态页, 玩家才知道"什么时候能填"而不是只看到一句报错。
    """
    survey = await SurveyService.get_survey_by_code(db, code)

    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")

    return ApiResponse(success=True, data=await _public_survey_payload(db, survey))


@router.post("/surveys/{code}/unlock", response_model=ApiResponse)
async def unlock_public_survey(
    code: str,
    data: SurveyUnlockRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """校验问卷访问口令并返回完整问卷（公开，无需认证）。

    口令是唯一的准入凭据, 故复用查询端点的 per-IP 限流挡住撞库式狂试;
    口令哈希的 pbkdf2 轮数本身也让单次尝试足够昂贵。无口令的卷直接按通过处理。
    """
    await check_query_rate_limit(get_real_ip(request))

    survey = await SurveyService.get_survey_by_code(db, code)
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")

    # 走 await 版: 20 万轮 pbkdf2 若在事件循环线程上同步跑, 这条未认证端点就是个免费的
    # 拒绝服务开关 —— 循环打口令即可让整个 API 排队
    if not await verify_access_password_async(data.password, survey.access_password_hash):
        logger.warning(f"[Unlock] 访问口令不正确: code={code}")
        raise HTTPException(status_code=403, detail="访问口令不正确")

    return ApiResponse(success=True, data=await _public_survey_payload(db, survey, unlocked=True))


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

    # 获取真实 IP 地址
    ip_address = get_real_ip(request)

    # === 安全检查 ===

    # 1. Turnstile 验证
    await verify_turnstile(data.turnstile_token, ip_address)

    # 2. IP 频率限制检查
    await check_ip_rate_limit(ip_address, code)

    # 3. 提交时间检测，同时获取填写耗时
    fill_duration = check_submit_time(data.start_time)

    # === 受理闸门 (可填状态 / 口令 / 同意声明 / 配额) ===

    # 4. 是否处于可填状态: 停用/未发布/未开始/已截止/名额已满 一律由 compute_availability 统一裁决,
    #    与详情接口同一套判定, 避免"列表说能填、提交说不能填"
    submission_count = await SurveyService.get_submission_count(db, survey.id)
    state, message = compute_availability(survey, submission_count, datetime.now(timezone.utc))
    if state != "open":
        logger.warning(f"[Submit] 问卷当前不可填: code={code}, state={state}")
        raise HTTPException(status_code=400, detail=message)

    # 5. 口令卷必须在提交时再验一次: 解锁接口只负责放题, 不在这里把关就能绕过解锁直接 POST
    if not await verify_access_password_async(data.access_password, survey.access_password_hash):
        logger.warning(f"[Submit] 访问口令不正确: code={code}")
        raise HTTPException(status_code=403, detail="访问口令不正确")

    # 6. 合规声明: 采集个人信息的卷必须有明确同意, 缺同意的提交不能受理
    if survey.require_consent and data.consent is not True:
        logger.warning(f"[Submit] 未勾选同意声明: code={code}")
        raise HTTPException(status_code=400, detail="请先阅读并同意声明")

    # 7. 每 IP 提交上限 (防同一人反复刷量; 取不到 IP 时 count 返回 0 即不限)
    if survey.max_submissions_per_ip is not None:
        ip_submitted = await SurveyService.count_submissions_by_ip(db, survey.id, ip_address)
        if ip_submitted >= survey.max_submissions_per_ip:
            logger.warning(f"[Submit] 超出每 IP 提交上限: code={code}, 该 IP 已提交 {ip_submitted} 次")
            raise HTTPException(
                status_code=429,
                detail=f"该问卷每个 IP 最多提交 {survey.max_submissions_per_ip} 次",
            )

    # 8. 总量上限复查: 第 4 步取的是快照, 之后还隔着几次 await, 并发提交可能已把名额填满,
    #    临门再数一次把穿透窗口压到最小 (文案与 availability 的 full 保持一致)
    if survey.max_submissions is not None:
        latest_count = await SurveyService.get_submission_count(db, survey.id)
        if latest_count >= survey.max_submissions:
            logger.warning(f"[Submit] 提交名额已满: code={code}, 现有 {latest_count} 条")
            raise HTTPException(
                status_code=400,
                detail=survey.closed_message or AVAILABILITY_MESSAGES["full"],
            )

    # === 业务逻辑验证 ===

    # 添加调试日志
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

    # 逐题校验已填内容的合法性 (选项范围/字数/数值区间/日期格式/评分上限/图片张数)。
    # 只校验可见题: 被条件隐藏的题本就不该提交答案, 拿隐藏分支里的残留答案去卡人是误伤。
    for answer in data.answers:
        question = question_map[answer.question_id]
        if not is_question_visible(question.condition, answer_map, question_map):
            continue
        try:
            validate_answer(question.type, answer.content, question.validation, question.options)
        except ValueError as exc:
            logger.warning(f"[Submit] 答案不合法: 题 {question.id} 「{question.title}」, 原因: {exc}")
            # 校验器只知道内容为何不合法(如"评分需在 1 - 5 之间"), 不知道是哪道题;
            # 长问卷里只回一句原因等于让玩家逐题猜, 必须把题目标题拼进去
            raise HTTPException(status_code=400, detail=f"「{question.title}」{exc}")

    # 检查必填问题（考虑条件题逻辑）
    # 对于随机问卷，前端只收到部分题目，无法在后端验证完整性
    if not survey.is_random:
        # 只检查可见的必填题 (condition.depends_on 语义=依赖题 question_id)
        required_questions = {
            q.id for q in survey.questions
            if q.is_required and is_question_visible(q.condition, answer_map, question_map)
        }
        # 只认"填了有效内容"的答案: 空 content / 空串 / 空数组一律不算作答,
        # 而判断题的 false 与数字题的 0 是有效答案, 交给 is_answered 按题型判定
        answered_questions = {
            a.question_id for a in data.answers
            if is_answered(question_map[a.question_id].type, a.content)
        }
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
                if not is_answered(question.type, content):
                    logger.warning(f"[Submit] 必填问题未作答: {question.title}")
                    raise HTTPException(
                        status_code=400,
                        detail=f"必填问题未作答: {question.title}"
                    )
    
    # === 按题目 role 标记抽取系统字段 (玩家名 / QQ) ===
    # 抽取实现统一走题型注册表的 answer_scalar: 它按 role_bindable 把关, 绑到多选/图片/判断题上
    # 一律抽不出值, 与管理端建题时的 _ROLE_EXTRACTABLE_TYPES 校验是同一份口径
    role_player_name = None
    role_qq = None
    for q in survey.questions:
        q_role = getattr(q, "role", None)
        if q_role == "player_name" and q.id in answer_map:
            role_player_name = answer_scalar(q.type, answer_map[q.id])
        elif q_role == "qq" and q.id in answer_map:
            role_qq = answer_scalar(q.type, answer_map[q.id])

    # 玩家名: 仅当问卷配了 role=player_name 题(白名单关联键)时才必填; 匿名收集表可空。
    requires_player_name = any(getattr(q, "role", None) == "player_name" for q in survey.questions)
    effective_player_name = (role_player_name or data.player_name or "").strip() or None
    if requires_player_name and not effective_player_name:
        raise HTTPException(
            status_code=400,
            detail="缺少玩家名: 请填写玩家名 (或在问卷中配置一道标记为玩家名的题)"
        )

    # QQ 若填写须为纯数字且长度合法 (机器人按 QQ @ 通知/主群准入; 上限 15 宽于真实 QQ、严于列宽 String(20))
    if role_qq and (not role_qq.isdigit() or len(role_qq) > 15):
        raise HTTPException(status_code=400, detail="QQ号需为纯数字且长度合法")

    # 创建提交（包含填写耗时 + 按 role 抽取的玩家名/QQ; 免审卷提交即终态见 create_submission）
    submission = await SubmissionService.create_submission(
        db, survey, data, ip_address, fill_duration,
        player_name=effective_player_name, qq=role_qq,
    )

    # 记录活动日志 (玩家名可空时用占位; ActivityLog.player_name 非空)
    await ActivityService.log_submit(db, effective_player_name or f"#{submission.id}", submission.id)

    # 入队审核群通知 (仅启用该动作的卷; 尽力而为: 入队失败不影响提交本身)
    if survey.action_notify_group:
        try:
            await bot_notify.enqueue(db, submission, bot_notify.SUBMIT)
        except Exception:
            await db.rollback()  # 清掉入队失败的脏会话, 不污染后续 (尽力而为, 不影响提交)
            logger.warning("入队 submit 通知失败 (不影响提交)", exc_info=True)

    # 推送 webhook (仅启用该动作的卷; 同样尽力而为: 第三方接收端不可用不能让已落库的提交报失败)
    # 传 data.answers 而非 submission.answers: 后者是关系属性, 异步会话里触发懒加载会抛 MissingGreenlet,
    # 而两者的 question_id/content 本就是同一份内容
    if survey.action_webhook and survey.webhook_url:
        try:
            await webhook.dispatch_submission(survey, submission, data.answers)
        except Exception:
            logger.warning("[Submit] webhook 推送失败 (不影响提交)", exc_info=True)

    # 记录 IP 提交（用于频率限制）
    await record_ip_submission(ip_address, code)

    return ApiResponse(
        success=True,
        data={
            "id": submission.id,
            # 自助凭据: 需审核卷凭此查询进度/领码; 免审收集表用不到, 前端据 requires_review 决定是否展示
            "token": submission.token,
            # 运营配了 success_message 就以它为准, 否则维持按是否需审核二选一的默认文案
            "message": survey.success_message or (
                "提交成功，感谢参与～" if not survey.review_required else "提交成功，请等待审核"
            ),
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

    # 仅启用发码动作的卷(白名单卷)可领码; 收集表无此动作直接拒绝
    survey = await SurveyService.get_survey_by_id(db, submission.survey_id)
    if not survey or not survey.action_issue_code:
        raise HTTPException(status_code=409, detail="该问卷不发放注册码")

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
