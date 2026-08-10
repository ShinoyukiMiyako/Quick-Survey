"""活动日志 API"""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.core import get_current_user, CurrentUser, iso_utc
from app.schemas import ApiResponse
from app.services import ActivityService


router = APIRouter(prefix="/activities", tags=["活动日志"])


@router.get("", response_model=ApiResponse)
async def get_activities(
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    action: Optional[str] = Query(default=None, description="筛选操作类型: submit, approved, rejected"),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """获取活动日志列表 (仅管理员; 日志含玩家名/操作者, 不对匿名公开)"""
    logs, total = await ActivityService.get_recent_activities(
        db, limit=limit, offset=offset, action=action
    )
    
    return ApiResponse(
        success=True,
        data={
            "logs": [
                {
                    "id": log.id,
                    "action": log.action,
                    "player_name": log.player_name,
                    "operator": log.operator,
                    "submission_id": log.submission_id,
                    "note": log.note,
                    "created_at": iso_utc(log.created_at),
                }
                for log in logs
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
