"""
QQ 机器人通知队列服务。

提交/审核时入队一条 BotNotification; NapCat 插件轮询取 pending、发到对应群后回调 ack。
入队是审核/提交主流程的"尽力而为"副作用 —— 入队失败不应让提交/审核失败 (调用方 try 包裹)。

投递目标与语义由问卷的 notify_group_id 决定, 见 audience_of。
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import BotNotification, Submission

logger = logging.getLogger(__name__)

# 通知类型
SUBMIT = "submit"
APPROVED = "approved"
REJECTED = "rejected"

# 通知受众。决定插件怎么发, 后端是唯一裁决方 —— 插件重装成本远高于后端重启, 判定逻辑
# 必须留在这边, 让插件退化成纯执行器。
AUDIENCE_USER = "user"    # 玩家向: 默认审核群 @ 提交者本人, 需其在群, submit 回填 in_review_group
AUDIENCE_ADMIN = "admin"  # 管理向: 播报到指定群, QQ 号写进正文, 不 @ 不查成员资格


def audience_of(survey) -> str:
    """该问卷的通知受众。配了 notify_group_id 即视为管理向。

    会专门指定一个群的场景就是运营侧收件箱 (招募表投到管理群), 群里没有提交者本人 ——
    @ 一个不在群的 QQ 在客户端只显示一串号码, 既送不达也不好看。反过来, 没配群号的
    白名单卷保持原样: 审核群 @ 本人。
    """
    return AUDIENCE_ADMIN if survey is not None and survey.notify_group_id is not None else AUDIENCE_USER


async def enqueue(
    db: AsyncSession,
    submission: Submission,
    type_: str,
    reason: Optional[str] = None,
) -> None:
    """入队一条通知。qq 缺失则跳过 (bot_notifications.qq 非空, 且无 QQ 也无从联系)。"""
    if not submission.qq:
        # 不能静默: 卷开了通知开关却没有 role=qq 的题时, 表现是"群里什么都没有"而无处可查
        logger.warning(
            "提交 #%s 无 QQ, 跳过 %s 通知入队 (该问卷缺少 role=qq 的题?)",
            submission.id, type_,
        )
        return
    db.add(BotNotification(
        submission_id=submission.id,
        qq=submission.qq,
        type=type_,
        reason=reason,
        status="pending",
    ))
    await db.commit()


async def list_pending(db: AsyncSession, limit: int = 20) -> list[BotNotification]:
    """取待发送通知 (按入队顺序)。

    连提交与问卷一并预载: 调用方要据问卷判定投递群与受众, 异步会话里碰懒加载会抛 MissingGreenlet。
    """
    result = await db.execute(
        select(BotNotification)
        .options(selectinload(BotNotification.submission).selectinload(Submission.survey))
        .where(BotNotification.status == "pending")
        .order_by(BotNotification.created_at.asc(), BotNotification.id.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def ack(db: AsyncSession, notification_id: int, in_group: Optional[bool] = None) -> bool:
    """
    插件回调: 标记通知已处理 (done)。

    submit 类型且带 in_group 时, 回填对应 Submission.in_review_group (面板据此标记"未在审核群")。
    管理向通知不带 in_group —— 那个群里本就没有提交者, 成员资格无意义。
    返回是否命中该 pending 通知。
    """
    notif = await db.get(BotNotification, notification_id)
    if not notif or notif.status != "pending":
        return False

    notif.status = "done"
    notif.sent_at = datetime.now(timezone.utc)

    if notif.type == SUBMIT and in_group is not None:
        submission = await db.get(Submission, notif.submission_id)
        if submission is not None:
            submission.in_review_group = in_group

    await db.commit()
    return True
