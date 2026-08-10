from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.core import get_current_user, CurrentUser, iso_utc
from app.models import Survey
from app.schemas import (
    ApiResponse,
    SurveyCreate,
    SurveyUpdate,
    SurveyReorderRequest,
    SurveyResponse,
    SurveyDetailResponse,
    QuestionCreate,
    QuestionUpdate,
    QuestionResponse,
)
from app.services import SurveyService, QuestionService, SubmissionService
from app.services.survey import build_submissions_csv, compute_availability
from app.services.analytics import build_survey_analytics


router = APIRouter(prefix="/surveys", tags=["问卷管理"])


# 与题型注册表的 role_bindable 对齐: 只有这四种题的答案是字符串标量,
# 绑到多选(values)/图片(images)/判断(布尔 value)题上必然抽不出玩家名。
_ROLE_EXTRACTABLE_TYPES = ("text", "short_text", "single", "select")


def _assert_player_name_extractable(survey: Survey) -> None:
    """
    启用问卷前确保玩家名能从答案里抽出来 (只对要拿玩家名去加白的卷生效)。

    公开端提交时不再收集顶层玩家名, 全靠 role=player_name 的题抽取; 抽不到就是整卷
    在玩家点提交的最后一步 400。启用这一刻不拦, 就只能等玩家来踩。
    """
    # 平台化后大量卷是纯收集(问卷/投票/报名), 它们既不加白也不需要玩家名, 再强制这道题
    # 就是把它们卡在"启用"这一步。只有白名单栏目或显式开了加白动作的卷才真的依赖玩家名。
    if not (survey.category == "whitelist" or survey.action_add_whitelist):
        return

    candidates = [q for q in survey.questions if q.role == "player_name"]

    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="启用前请先把一道题的绑定字段设为「玩家名」, 否则玩家提交时会因取不到玩家名而失败",
        )
    if len(candidates) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"有 {len(candidates)} 道题都绑定了「玩家名」, 请只保留一道",
        )

    question = candidates[0]
    if question.type not in _ROLE_EXTRACTABLE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"玩家名题「{question.title}」是 {question.type} 题, 抽不出文本, 请改为文本题或单选/下拉题",
        )
    if not question.is_required:
        raise HTTPException(
            status_code=400,
            detail=f"玩家名题「{question.title}」必须设为必填, 否则玩家跳过后无法提交",
        )
    if question.condition:
        raise HTTPException(
            status_code=400,
            detail=f"玩家名题「{question.title}」不能配条件显示, 被隐藏时它的答案不会随提交上传",
        )


def _survey_settings_payload(survey: Survey) -> dict:
    """列表与详情共用的开放窗口/门禁/文案设置片段。

    两处各手写一份必然漂移 —— surveys 表以后再加设置列时只会有人记得改其中一处。
    另: 口令哈希绝不出现在任何一份管理端响应里, 对外只暴露"有没有设口令"这一个布尔。
    """
    return {
        "starts_at": iso_utc(survey.starts_at),
        "ends_at": iso_utc(survey.ends_at),
        "max_submissions": survey.max_submissions,
        "max_submissions_per_ip": survey.max_submissions_per_ip,
        "require_consent": survey.require_consent,
        "privacy_notice": survey.privacy_notice,
        "closed_message": survey.closed_message,
        "success_message": survey.success_message,
        "action_webhook": survey.action_webhook,
        "webhook_url": survey.webhook_url,
        "notify_group_id": survey.notify_group_id,
        "has_access_password": survey.access_password_hash is not None,
    }


@router.get("/stats/overview", response_model=ApiResponse)
async def get_survey_stats(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """获取问卷统计概览"""
    stats = await SurveyService.get_survey_stats(db)
    return ApiResponse(
        success=True,
        data=stats
    )


@router.post("", response_model=ApiResponse)
async def create_survey(
    data: SurveyCreate,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """创建问卷"""
    survey = await SurveyService.create_survey(db, data, user.id)
    return ApiResponse(
        success=True,
        data={
            "id": survey.id,
            "code": survey.code,
            "title": survey.title,
        }
    )


@router.get("", response_model=ApiResponse)
async def get_surveys(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    category: Optional[str] = Query(None, description="按栏目过滤: whitelist / collection"),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """获取问卷列表 (按 置顶 > 排序位 > 创建时间)"""
    surveys, total = await SurveyService.get_surveys(db, page, size, search, is_active, category)

    items = []
    for survey in surveys:
        question_count = await SurveyService.get_question_count(db, survey.id)
        submission_count = await SurveyService.get_submission_count(db, survey.id)
        items.append({
            "id": survey.id,
            "title": survey.title,
            "description": survey.description,
            "code": survey.code,
            "is_active": survey.is_active,
            "is_random": survey.is_random,
            "random_count": survey.random_count,
            "sort_order": survey.sort_order,
            "is_pinned": survey.is_pinned,
            "category": survey.category,
            "visibility": survey.visibility,
            "status": survey.status,
            "cover_url": survey.cover_url,
            "icon": survey.icon,
            "theme_color": survey.theme_color,
            "summary": survey.summary,
            "estimated_minutes": survey.estimated_minutes,
            **_survey_settings_payload(survey),
            "question_count": question_count,
            "submission_count": submission_count,
            "created_at": iso_utc(survey.created_at),
            "updated_at": iso_utc(survey.updated_at),
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


@router.patch("/reorder", response_model=ApiResponse)
async def reorder_surveys(
    data: SurveyReorderRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """批量重排问卷展示顺序 (拖拽排序落库)。

    注意: 本路由必须声明在 /{survey_id} 之前, 否则 'reorder' 会被当作 survey_id 解析。
    """
    updated = await SurveyService.reorder_surveys(db, data.orders)
    return ApiResponse(
        success=True,
        data={"updated": updated},
    )


@router.get("/{survey_id}", response_model=ApiResponse)
async def get_survey(
    survey_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """获取问卷详情"""
    survey = await SurveyService.get_survey_by_id(db, survey_id)
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")

    questions = sorted(survey.questions, key=lambda q: q.order)

    # 名额上限的判定要用实时提交数, 面板据此显示"名额已满"而不是只看时间窗
    submission_count = await SurveyService.get_submission_count(db, survey.id)
    state, availability_message = compute_availability(
        survey, submission_count, datetime.now(timezone.utc)
    )

    return ApiResponse(
        success=True,
        data={
            "id": survey.id,
            "title": survey.title,
            "description": survey.description,
            "code": survey.code,
            "is_active": survey.is_active,
            "is_random": survey.is_random,
            "random_count": survey.random_count,
            "sort_order": survey.sort_order,
            "is_pinned": survey.is_pinned,
            "category": survey.category,
            "visibility": survey.visibility,
            "status": survey.status,
            "cover_url": survey.cover_url,
            "icon": survey.icon,
            "theme_color": survey.theme_color,
            "summary": survey.summary,
            "estimated_minutes": survey.estimated_minutes,
            "review_required": survey.review_required,
            "action_add_whitelist": survey.action_add_whitelist,
            "action_issue_code": survey.action_issue_code,
            "action_notify_group": survey.action_notify_group,
            **_survey_settings_payload(survey),
            "availability": {"state": state, "message": availability_message},
            "questions": [
                {
                    "id": q.id,
                    "title": q.title,
                    "description": q.description,
                    "type": q.type,
                    "options": q.options,
                    "is_required": q.is_required,
                    "is_pinned": q.is_pinned,
                    "order": q.order,
                    "validation": q.validation,
                    "condition": q.condition,
                    # 必须下发: 管理端保存已有题时按 `role: q.role ?? null` 回传, 这里不给
                    # 就会被显式解绑成 NULL, 形成"配一次、下次保存即丢"的自毁回环。
                    "role": q.role,
                }
                for q in questions
            ],
            "created_at": iso_utc(survey.created_at),
            "updated_at": iso_utc(survey.updated_at),
        }
    )


@router.patch("/{survey_id}", response_model=ApiResponse)
async def update_survey(
    survey_id: int,
    data: SurveyUpdate,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """更新问卷"""
    survey = await SurveyService.get_survey_by_id(db, survey_id)
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")

    # 只在显式启用这一刻校验: 面板保存问卷时先 PATCH 基本信息再逐题增删, 那一步的
    # 请求体不含 is_active, 此时题目尚未落定, 在那里校验会误伤正常的保存流程。
    if data.model_dump(exclude_unset=True).get("is_active") is True:
        _assert_player_name_extractable(survey)

    survey = await SurveyService.update_survey(db, survey, data)
    
    return ApiResponse(
        success=True,
        data={"id": survey.id, "message": "更新成功"}
    )


@router.delete("/{survey_id}", response_model=ApiResponse)
async def delete_survey(
    survey_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """删除问卷"""
    survey = await SurveyService.get_survey_by_id(db, survey_id)
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")
    
    await SurveyService.delete_survey(db, survey)
    
    return ApiResponse(
        success=True,
        data={"message": "删除成功"}
    )


@router.get("/{survey_id}/export")
async def export_survey_submissions(
    survey_id: int,
    status: Optional[str] = Query(
        None,
        pattern="^(pending|approved|rejected)$",
        description="只导出该状态的提交, 缺省导出全部",
    ),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """导出某问卷的提交为 CSV (收集表结果导出; 每题一列, 含 Excel BOM)。"""
    survey = await SurveyService.get_survey_by_id(db, survey_id)
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")

    submissions = await SubmissionService.get_submissions_with_answers(db, survey_id)
    if status:
        # 取数方法是导出专用的"整卷带答案"查询, 没有状态维度; 导出本就是低频操作,
        # 在 Python 侧筛一遍即可, 不为此改动被其它调用方共用的服务方法。
        submissions = [sub for sub in submissions if sub.status == status]
    csv_text = build_submissions_csv(survey, submissions)

    # 文件名带上状态, 免得同一卷的"全部"与"已通过"两份导出在下载目录里互相覆盖
    suffix = f"_{status}" if status else ""
    filename = f"survey_{survey_id}_submissions{suffix}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{survey_id}/duplicate", response_model=ApiResponse)
async def duplicate_survey(
    survey_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """复制问卷 (含全部题目与分支条件, 不含提交数据), 副本落成未启用的草稿。"""
    survey = await SurveyService.get_survey_by_id(db, survey_id)
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")

    clone = await SurveyService.duplicate_survey(db, survey_id, user.id)

    return ApiResponse(
        success=True,
        data={
            "id": clone.id,
            "code": clone.code,
            "title": clone.title,
        }
    )


@router.get("/{survey_id}/analytics", response_model=ApiResponse)
async def get_survey_analytics(
    survey_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """问卷统计报表 (总量 / 状态分布 / 平均耗时 / 每日趋势 / 逐题分布)。"""
    survey = await SurveyService.get_survey_by_id(db, survey_id)
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")

    return ApiResponse(
        success=True,
        data=await build_survey_analytics(db, survey)
    )


# ==================== 问题管理 ====================

@router.post("/{survey_id}/questions", response_model=ApiResponse)
async def add_question(
    survey_id: int,
    data: QuestionCreate,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """添加问题"""
    # DEBUG: 打印接收到的数据
    print(f"[DEBUG] add_question received: is_pinned={data.is_pinned}, data={data.model_dump()}")
    
    survey = await SurveyService.get_survey_by_id(db, survey_id)
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")
    
    question = await QuestionService.add_question(db, survey_id, data)
    
    return ApiResponse(
        success=True,
        data={
            "id": question.id,
            "title": question.title,
            "type": question.type,
            "is_pinned": question.is_pinned,
            "role": question.role,
        }
    )


@router.patch("/{survey_id}/questions/{question_id}", response_model=ApiResponse)
async def update_question(
    survey_id: int,
    question_id: int,
    data: QuestionUpdate,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """更新问题"""
    question = await QuestionService.get_question_by_id(db, question_id)
    if not question or question.survey_id != survey_id:
        raise HTTPException(status_code=404, detail="问题不存在")
    
    question = await QuestionService.update_question(db, question, data)
    
    return ApiResponse(
        success=True,
        data={"id": question.id, "message": "更新成功"}
    )


@router.delete("/{survey_id}/questions/{question_id}", response_model=ApiResponse)
async def delete_question(
    survey_id: int,
    question_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """删除问题"""
    question = await QuestionService.get_question_by_id(db, question_id, load_answers=True)
    if not question or question.survey_id != survey_id:
        raise HTTPException(status_code=404, detail="问题不存在")
    
    await QuestionService.delete_question(db, question)
    
    return ApiResponse(
        success=True,
        data={"message": "删除成功"}
    )
