"""
机器人通知队列 + 按 QQ 查过审 的单元测试。
"""
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.db import Base
from app.models import Survey
from app.schemas import SubmissionCreate
from app.services import SubmissionService
from app.services import bot_notify


async def _make_session(tmp_path) -> AsyncSession:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


async def _make_survey(session: AsyncSession, notify_group_id=None, title="测试问卷", code="testcode") -> Survey:
    survey = Survey(title=title, code=code, is_active=True, notify_group_id=notify_group_id)
    session.add(survey)
    await session.commit()
    await session.refresh(survey)
    return survey


async def _make_submission(session, survey, player_name, qq, status="pending"):
    sub = await SubmissionService.create_submission(
        session, survey, SubmissionCreate(answers=[]), player_name=player_name, qq=qq
    )
    if status != "pending":
        sub.status = status
        sub.reviewed_at = datetime.now(timezone.utc)
        await session.commit()
    return sub


async def test_get_approved_by_qq(tmp_path):
    session = await _make_session(tmp_path)
    survey = await _make_survey(session)
    await _make_submission(session, survey, "Alice", "10001", status="approved")
    await _make_submission(session, survey, "Bob", "10002", status="pending")
    await _make_submission(session, survey, "Carol", "10003", status="rejected")

    hit = await SubmissionService.get_approved_by_qq(session, "10001")
    assert hit is not None and hit.player_name == "Alice"
    # pending / rejected / 不存在 都不算命中
    assert await SubmissionService.get_approved_by_qq(session, "10002") is None
    assert await SubmissionService.get_approved_by_qq(session, "10003") is None
    assert await SubmissionService.get_approved_by_qq(session, "99999") is None
    await session.close()


async def test_enqueue_list_ack_flow_and_in_review_group(tmp_path):
    session = await _make_session(tmp_path)
    survey = await _make_survey(session)
    sub = await _make_submission(session, survey, "Dave", "20001")

    await bot_notify.enqueue(session, sub, bot_notify.SUBMIT)
    pending = await bot_notify.list_pending(session)
    assert len(pending) == 1 and pending[0].type == "submit" and pending[0].qq == "20001"

    # ack 回填 in_review_group=False
    nid = pending[0].id
    assert await bot_notify.ack(session, nid, in_group=False) is True
    await session.refresh(sub)
    assert sub.in_review_group is False
    # 已处理 -> 不再 pending; 重复 ack 返回 False
    assert await bot_notify.list_pending(session) == []
    assert await bot_notify.ack(session, nid, in_group=True) is False
    await session.close()


async def test_enqueue_rejected_carries_reason_and_skips_when_no_qq(tmp_path):
    session = await _make_session(tmp_path)
    survey = await _make_survey(session)

    sub = await _make_submission(session, survey, "Eve", "20002")
    await bot_notify.enqueue(session, sub, bot_notify.REJECTED, reason="刷屏")
    pending = await bot_notify.list_pending(session)
    assert len(pending) == 1 and pending[0].type == "rejected" and pending[0].reason == "刷屏"

    # 无 QQ 的提交不入队
    no_qq = await _make_submission(session, survey, "Frank", None)
    await bot_notify.enqueue(session, no_qq, bot_notify.SUBMIT)
    assert len(await bot_notify.list_pending(session)) == 1  # 仍是 1, 没新增
    await session.close()


async def test_audience_follows_notify_group_id(tmp_path):
    """通知受众由问卷的 notify_group_id 单字段裁决 —— 插件据此决定 @ 本人还是纯文本播报。"""
    session = await _make_session(tmp_path)
    plain = await _make_survey(session)
    routed = await _make_survey(session, notify_group_id=1038077608, title="招募", code="hire")

    assert bot_notify.audience_of(plain) == bot_notify.AUDIENCE_USER
    assert bot_notify.audience_of(routed) == bot_notify.AUDIENCE_ADMIN
    # 问卷已被删 (历史孤儿通知) 时不能炸, 按玩家向兜底
    assert bot_notify.audience_of(None) == bot_notify.AUDIENCE_USER
    await session.close()


async def test_list_pending_preloads_submission_and_survey(tmp_path):
    """list_pending 必须预载 submission->survey。

    内部接口要读 survey.notify_group_id 才能告诉插件发去哪个群; 懒加载在异步会话里会抛
    MissingGreenlet, 表现是整个轮询端点 500, 通知全线停摆。这里断言链路可直接取到。
    """
    session = await _make_session(tmp_path)
    survey = await _make_survey(session, notify_group_id=1038077608, title="技术组招募", code="techhire")
    sub = await _make_submission(session, survey, "Grace", "20003")
    await bot_notify.enqueue(session, sub, bot_notify.SUBMIT)

    # 清掉身份映射, 强制 list_pending 自己把关系查出来 (否则会命中会话缓存, 测不出漏预载)
    session.expunge_all()

    pending = await bot_notify.list_pending(session)
    assert len(pending) == 1
    loaded = pending[0]
    assert loaded.submission is not None
    assert loaded.submission.survey is not None
    assert loaded.submission.survey.notify_group_id == 1038077608
    assert loaded.submission.survey.title == "技术组招募"
    assert loaded.submission.player_name == "Grace"
    await session.close()


async def test_internal_notifications_payload_carries_routing(tmp_path):
    """内部接口逐条下发 group_id / audience / 问卷名 / 提交人, 插件只按字段执行不做判定。"""
    from app.api.internal import get_notifications

    session = await _make_session(tmp_path)
    routed = await _make_survey(session, notify_group_id=1038077608, title="技术组招募", code="techhire")
    plain = await _make_survey(session, title="入群问题", code="whitelist1")

    admin_sub = await _make_submission(session, routed, "Grace", "20003")
    user_sub = await _make_submission(session, plain, "Heidi", "20004")
    await bot_notify.enqueue(session, admin_sub, bot_notify.SUBMIT)
    await bot_notify.enqueue(session, user_sub, bot_notify.REJECTED, reason="资料不全")

    resp = await get_notifications(limit=20, db=session)
    items = {n["qq"]: n for n in resp.data["notifications"]}
    assert len(items) == 2

    admin = items["20003"]
    assert admin["group_id"] == 1038077608
    assert admin["audience"] == "admin"
    assert admin["survey_title"] == "技术组招募"
    assert admin["player_name"] == "Grace"
    assert admin["type"] == "submit"

    user = items["20004"]
    # 没配群号 -> 不指定目标群, 插件回落到自己配置的审核群
    assert user["group_id"] is None
    assert user["audience"] == "user"
    assert user["survey_title"] == "入群问题"
    assert user["reason"] == "资料不全"
    await session.close()
