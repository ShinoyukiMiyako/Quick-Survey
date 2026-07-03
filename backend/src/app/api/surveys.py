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
