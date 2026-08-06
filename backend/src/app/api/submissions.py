import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.core import get_current_user, CurrentUser
from app.schemas import ApiResponse, SubmissionReview
# BulkReviewRequest 未列进 app.schemas 的桶导出, 从定义模块直接取, 不依赖桶文件的更新
from app.schemas.schemas import BulkReviewRequest
from app.services import SubmissionService, SurveyService, CleanupService, ActivityService
from app.services import bot_notify
from app.services.ip_location import lookup as resolve_ip_location
from app.models import Question

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/submissions", tags=["提交管理"])


# 注意：stats 路由必须放在 /{submission_id} 之前，避免路由冲突
@router.post("/cleanup", response_model=ApiResponse)
async def run_cleanup(
    user: CurrentUser = Depends(get_current_user),
):
    """
    手动触发清理任务
    清理已审核提交的图片文件，保留提交记录元数据与答案文本内容
    """
    stats = await CleanupService.run_cleanup()
    
    # 格式化释放的空间
    bytes_freed = stats["bytes_freed"]
    if bytes_freed >= 1024 * 1024:
        freed_str = f"{bytes_freed / (1024 * 1024):.2f} MB"
    elif bytes_freed >= 1024:
        freed_str = f"{bytes_freed / 1024:.2f} KB"
    else:
        freed_str = f"{bytes_freed} bytes"
    
    return ApiResponse(
        success=True,
        data={
            "submissions_cleaned": stats["submissions_cleaned"],
            "images_cleared": stats["images_cleared"],
            "files_deleted": stats["files_deleted"],
            "orphan_files_deleted": stats["orphan_files_deleted"],
            "space_freed": freed_str,
        },
        message=f"清理完成，释放空间: {freed_str}"
    )


@router.get("/stats/overview", response_model=ApiResponse)
async def get_stats(
    category: Optional[str] = Query(None, description="按栏目过滤(审核页传 whitelist 只统计白名单卷)"),
    review_required: Optional[bool] = Query(None, description="按是否需人工审核过滤(审核页统计传 true)"),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """获取统计概览。

    收集表也能手动开人工审核, 单靠 category=whitelist 统计会把这类卷的待审量算丢,
    面板上的待审计数与列表口径就对不上; review_required 是按"要不要人审"直接圈队列的口径。
    """
    _, pending_count = await SubmissionService.get_submissions(
        db, 1, 1, "pending", category=category, review_required=review_required
    )
    _, approved_count = await SubmissionService.get_submissions(
        db, 1, 1, "approved", category=category, review_required=review_required
    )
    _, rejected_count = await SubmissionService.get_submissions(
        db, 1, 1, "rejected", category=category, review_required=review_required
    )

    return ApiResponse(
        success=True,
        data={
            "pending": pending_count,
            "approved": approved_count,
            "rejected": rejected_count,
            "total": pending_count + approved_count + rejected_count,
        }
    )


@router.get("", response_model=ApiResponse)
async def get_submissions(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None, pattern="^(pending|approved|rejected)$"),
    survey_id: Optional[int] = None,
    player_name: Optional[str] = None,
    category: Optional[str] = Query(None, description="按栏目过滤: whitelist=审核队列 / collection=收集表结果"),
    review_required: Optional[bool] = Query(None, description="按是否需人工审核过滤(审核队列传 true)"),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """获取提交列表。审核队列传 review_required=true; 收集表结果传 survey_id。

    审核队列不能再按 category=whitelist 圈: 收集表现在也允许手动开人工审核,
    这类卷的 pending 提交会被栏目过滤挡在审核页外, 玩家永远等不到人审。
    """
    submissions, total = await SubmissionService.get_submissions(
        db, page, size, status, survey_id, player_name, category, review_required
    )

    items = []
    for sub in submissions:
        items.append({
            "id": sub.id,
            "survey_id": sub.survey_id,
            "survey_title": sub.survey.title if sub.survey else "",
            # 批量审核要据此决定是否加白: 只有详情下发会让面板对着列表勾选时无从判断,
            # 把关掉加白动作的卷也一并写进 MC 白名单 (列表查询已 selectinload survey, 不会 N+1)
            "survey_add_whitelist": sub.survey.action_add_whitelist if sub.survey else True,
            "player_name": sub.player_name,
            "qq": sub.qq,
            "status": sub.status,
            "in_review_group": sub.in_review_group,  # True/False/null, 面板标记"未在审核群"
            "created_at": sub.created_at.isoformat(),
            "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
        })
    
    return ApiResponse(
        success=True,
        data={
            "items": items,
            "page": page,
            "size": size,
            "total": total,
            "pages": (total + size - 1) // size,
        }
    )


async def _apply_review(
    db: AsyncSession,
    submission_id: int,
    data: SubmissionReview,
    user: CurrentUser,
):
    """单条审核的全部副作用: 落审核状态 + 记活动日志 + 入队审核群通知。

    抽成一处是为了让批量审核走同一条路径 —— 批量端点若自带一份实现, 以后改通知门控或
    日志字段时只会有人改单条这一处, 批量就静默漏掉。业务前置条件不满足时抛 HTTPException,
    由调用方决定是直接 400/404 还是记进批量结果。
    """
    submission = await SubmissionService.get_submission_by_id(db, submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="提交不存在")

    if submission.status != "pending":
        raise HTTPException(status_code=400, detail="该提交已被审核")

    player_name = submission.player_name
    submission = await SubmissionService.review_submission(db, submission, data, user.id)

    # 记录活动日志
    await ActivityService.log_review(
        db,
        player_name=player_name,
        submission_id=submission_id,
        status=data.status,
        operator=user.username,
        note=data.review_note,
    )

    # 入队审核群通知 (仅启用该动作的卷; 尽力而为: 入队失败不影响审核结果)
    if submission.survey and submission.survey.action_notify_group:
        try:
            if data.status == "approved":
                await bot_notify.enqueue(db, submission, bot_notify.APPROVED)
            else:
                await bot_notify.enqueue(db, submission, bot_notify.REJECTED, reason=data.review_note)
        except Exception:
            await db.rollback()  # 清掉入队失败的脏会话 (尽力而为, 不影响审核结果)
            logger.warning("入队审核通知失败 (不影响审核)", exc_info=True)

    return submission


@router.patch("/bulk-review", response_model=ApiResponse)
async def bulk_review_submissions(
    data: BulkReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """批量审核 (面板勾选多条后一次通过/拒绝), 整批共用一条备注。

    注意: 本路由必须声明在 /{submission_id} 之前, 否则 'bulk-review' 会被当作 submission_id 解析。
    """
    review = SubmissionReview(status=data.status, review_note=data.review_note)

    updated = 0
    results = []
    for submission_id in data.ids:
        try:
            await _apply_review(db, submission_id, review, user)
        except HTTPException as exc:
            # "已被审核"/"不存在"是批量场景的正常结果(别人刚审过、列表不新鲜),
            # 逐条记原因继续, 不能让一条把其余几十条一起废掉
            results.append({"id": submission_id, "ok": False, "error": str(exc.detail)})
            continue
        except Exception as exc:
            # 意外错误必须带栈留痕, 并回滚脏会话, 否则后面几条会在坏会话上连环失败
            await db.rollback()
            logger.warning("批量审核第 %s 条失败", submission_id, exc_info=True)
            results.append({"id": submission_id, "ok": False, "error": str(exc)})
            continue

        updated += 1
        results.append({"id": submission_id, "ok": True, "error": None})

    return ApiResponse(
        success=True,
        data={
            "updated": updated,
            "results": results,
        }
    )


@router.get("/{submission_id}", response_model=ApiResponse)
async def get_submission(
    submission_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """获取提交详情"""
    # 获取提交并标记首次查看时间
    submission = await SubmissionService.get_submission_by_id(db, submission_id, mark_viewed=True)
    if not submission:
        raise HTTPException(status_code=404, detail="提交不存在")
    
    # 获取问卷以获取问题信息
    survey = await SurveyService.get_survey_by_id(db, submission.survey_id)
    questions_map = {q.id: q for q in survey.questions} if survey else {}
    
    answers = []
    for answer in submission.answers:
        question = questions_map.get(answer.question_id)
        answers.append({
            "id": answer.id,
            "question_id": answer.question_id,
            "question_title": question.title if question else "",
            "question_type": question.type if question else "",
            "question_options": question.options if question else None,  # 选项列表，用于前端渲染
            "question_role": question.role if question else None,  # 语义标记, 供面板识别玩家名/QQ 行
            "content": answer.content,
        })
    
    return ApiResponse(
        success=True,
        data={
            "id": submission.id,
            "survey_id": submission.survey_id,
            "survey_title": submission.survey.title if submission.survey else "",
            # 场景动作: 面板据此决定通过时是否加白 (收集表不加白)
            "survey_category": submission.survey.category if submission.survey else "whitelist",
            "survey_add_whitelist": submission.survey.action_add_whitelist if submission.survey else True,
            "player_name": submission.player_name,
            "qq": submission.qq,
            "ip_address": submission.ip_address,
            "ip_location": resolve_ip_location(submission.ip_address),  # 离线 ip2region 解析, 无数据则 null
            "fill_duration": submission.fill_duration,  # 填写耗时
            "first_viewed_at": submission.first_viewed_at.isoformat() if submission.first_viewed_at else None,  # 首次查看时间
            "status": submission.status,
            "review_note": submission.review_note,
            "answers": answers,
            "created_at": submission.created_at.isoformat(),
            "reviewed_at": submission.reviewed_at.isoformat() if submission.reviewed_at else None,
            "reviewed_by": submission.reviewed_by,
        }
    )


@router.patch("/{submission_id}/review", response_model=ApiResponse)
async def review_submission(
    submission_id: int,
    data: SubmissionReview,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """审核提交"""
    submission = await _apply_review(db, submission_id, data, user)

    return ApiResponse(
        success=True,
        data={
            "id": submission.id,
            "status": submission.status,
            "message": "审核成功",
        }
    )
