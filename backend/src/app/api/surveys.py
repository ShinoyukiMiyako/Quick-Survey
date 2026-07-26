from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.core import get_current_user, CurrentUser
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
from app.services import SurveyService, QuestionService


router = APIRouter(prefix="/surveys", tags=["问卷管理"])


# 与 public.py 的 _role_scalar 抽取规则对应: 只有 text/single 题的答案是字符串标量,
# 绑到多选(values)/图片(images)/判断(布尔 value)题上必然抽不出玩家名。
_ROLE_EXTRACTABLE_TYPES = ("text", "single")


def _assert_player_name_extractable(survey: Survey) -> None:
    """
    启用问卷前确保玩家名能从答案里抽出来。

    公开端提交时不再收集顶层玩家名, 全靠 role=player_name 的题抽取; 抽不到就是整卷
    在玩家点提交的最后一步 400。启用这一刻不拦, 就只能等玩家来踩。
    """
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
            detail=f"玩家名题「{question.title}」是 {question.type} 题, 抽不出文本, 请改为文本题或单选题",
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
            "question_count": question_count,
            "submission_count": submission_count,
            "created_at": survey.created_at.isoformat(),
            "updated_at": survey.updated_at.isoformat(),
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
            "created_at": survey.created_at.isoformat(),
            "updated_at": survey.updated_at.isoformat(),
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
