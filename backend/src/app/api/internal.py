"""
内部接口 (仅供 NapCat 插件调用, X-Internal-Token 鉴权, 非公开)。

- GET  /internal/approved?qq=        主群自动准入: 该 QQ 是否有已过审提交
- GET  /internal/notifications        插件轮询: 取待发送的审核群通知
- POST /internal/notifications/{id}/ack  插件回调: 标记已发, 回填 in_review_group
"""
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Submission
from app.schemas import ApiResponse
from app.services import SubmissionService
from app.services import bot_notify
from app.core.config import get_settings


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """datetime -> UTC ISO 字符串; naive 视为 UTC。供插件按北京时间展示。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


async def require_internal_token(
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
):
    """X-Internal-Token 常量时间比对。token 未配置或不匹配一律 401 (fail-closed)。"""
    token = get_settings().internal.token
    # 转 bytes 比较: compare_digest 对含非 ASCII 的 str 会抛 TypeError -> 500; bytes 恒安全且常量时间
    if (
        not token
        or not x_internal_token
        or not secrets.compare_digest(x_internal_token.encode("utf-8"), token.encode("utf-8"))
    ):
        raise HTTPException(status_code=401, detail="无效的内部凭证")


router = APIRouter(
    prefix="/internal",
    tags=["内部接口"],
    dependencies=[Depends(require_internal_token)],
)


class AckBody(BaseModel):
    in_group: Optional[bool] = None  # submit 类型回填: 该 QQ 是否在审核群


@router.get("/approved", response_model=ApiResponse)
async def check_approved(
    qq: str = Query(..., min_length=1, max_length=20),
    db: AsyncSession = Depends(get_db),
):
    """该 QQ 是否有已过审提交 (主群加群申请自动准入判定)。"""
    submission = await SubmissionService.get_approved_by_qq(db, qq)
    # 最小披露: 只回布尔 (插件仅需此判定); 不回 player_name, 避免内部端点成为 QQ->真实玩家名 oracle
    return ApiResponse(success=True, data={"approved": submission is not None})


@router.get("/notifications", response_model=ApiResponse)
async def get_notifications(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """取待发送的群通知 (插件轮询消费)。

    每条自带 group_id 与 audience: 发去哪个群、@ 本人还是纯文本播报, 一律由后端裁决,
    插件只按字段执行。插件重装要 patch NapCat 白名单并重启 (有丢登录态的风险), 决策留在
    这边才能改一次配置就生效。
    """
    items = await bot_notify.list_pending(db, limit)
    payload = []
    for n in items:
        submission = n.submission
        survey = submission.survey if submission is not None else None
        payload.append({
            "id": n.id,
            "qq": n.qq,
            "type": n.type,
            "reason": n.reason,
            "submission_id": n.submission_id,
            "created_at": _iso_utc(n.created_at),
            # 投递目标群; null = 插件用自己配置的默认审核群
            "group_id": survey.notify_group_id if survey is not None else None,
            "audience": bot_notify.audience_of(survey),
            # 管理向播报要在正文里点名是哪份卷、谁提交的 —— 管理群同时收多个招募表的通知
            "survey_title": survey.title if survey is not None else None,
            "player_name": submission.player_name if submission is not None else None,
        })
    return ApiResponse(success=True, data={"notifications": payload})


@router.post("/notifications/{notification_id}/ack", response_model=ApiResponse)
async def ack_notification(
    notification_id: int,
    body: AckBody,
    db: AsyncSession = Depends(get_db),
):
    """标记通知已处理; submit 类型带 in_group 时回填 Submission.in_review_group。"""
    acked = await bot_notify.ack(db, notification_id, body.in_group)
    return ApiResponse(success=True, data={"acked": acked})


@router.get("/stats", response_model=ApiResponse)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """运维状态: 当前待审核问卷数 (供 NapCat 插件 #状态 命令展示)。"""
    result = await db.execute(
        select(func.count()).select_from(Submission).where(Submission.status == "pending")
    )
    return ApiResponse(success=True, data={"pending_review": int(result.scalar_one())})
