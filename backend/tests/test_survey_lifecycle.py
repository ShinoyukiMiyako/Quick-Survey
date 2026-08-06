"""阶段3 问卷生命周期 / 访问口令 / 复制 的核心逻辑测试。

覆盖 compute_availability 的状态机与优先级、访问口令的哈希往返与 fail-closed、
create_survey 对新增设置字段的落库、以及 duplicate_survey 的条件 question_id 重映射。
删掉对应逻辑这些断言必须挂掉。
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.db import Base
from app.models import Question, Submission
from app.services.survey import (
    AVAILABILITY_MESSAGES,
    SubmissionService,
    SurveyService,
    _as_naive_utc,
    compute_availability,
    hash_access_password,
    hash_access_password_async,
    verify_access_password,
    verify_access_password_async,
)
from app.schemas import SurveyCreate, SurveyUpdate


# 固定时间基准: 状态机全靠时间比较, 用真实 now 会让"刚好卡在边界"的用例随机翻车
NOW = datetime(2026, 8, 6, 12, 0, 0)


class _SurveyStub:
    """compute_availability 只读这几个属性; 用轻量 stub 免去建库开销。"""

    def __init__(self, **kw):
        self.is_active = kw.get("is_active", True)
        self.status = kw.get("status", "published")
        self.starts_at = kw.get("starts_at")
        self.ends_at = kw.get("ends_at")
        self.max_submissions = kw.get("max_submissions")
        self.closed_message = kw.get("closed_message")


async def _session(tmp_path) -> AsyncSession:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


async def _seed_branching_survey(session) -> tuple[int, int]:
    """建一张三题的卷: 后两题分别用新/旧两种条件形态依赖第一题。返回 (survey_id, 第一题 id)。"""
    survey = await SurveyService.create_survey(
        session, SurveyCreate(title="原卷", category="collection", questions=[])
    )
    q1 = Question(
        survey_id=survey.id, title="玩过吗", type="single", order=0,
        options=[{"value": "yes", "label": "玩过"}, {"value": "no", "label": "没玩过"}],
    )
    session.add(q1)
    await session.commit()
    await session.refresh(q1)

    session.add_all([
        Question(
            survey_id=survey.id, title="玩了多久", type="short_text", order=1,
            condition={
                "action": "show", "match": "all",
                "rules": [{"question_id": q1.id, "operator": "eq", "value": "yes"}],
            },
        ),
        Question(
            survey_id=survey.id, title="旧形态分支", type="text", order=2,
            condition={"depends_on": q1.id, "show_when": "yes"},
        ),
    ])
    await session.commit()
    return survey.id, q1.id


# ==================== compute_availability ====================

def test_availability_open():
    assert compute_availability(_SurveyStub(), 0, NOW) == ("open", None)


def test_availability_inactive():
    state, message = compute_availability(_SurveyStub(is_active=False), 0, NOW)
    assert state == "inactive"
    assert message == AVAILABILITY_MESSAGES["inactive"]


def test_availability_unpublished():
    state, message = compute_availability(_SurveyStub(status="draft"), 0, NOW)
    assert state == "unpublished"
    assert message == AVAILABILITY_MESSAGES["unpublished"]

    assert compute_availability(_SurveyStub(status="archived"), 0, NOW)[0] == "unpublished"


def test_availability_not_started():
    state, message = compute_availability(_SurveyStub(starts_at=NOW + timedelta(seconds=1)), 0, NOW)
    assert state == "not_started"
    assert message == AVAILABILITY_MESSAGES["not_started"]
    # 起始时刻本身算已开放 (starts_at 是闭区间下界)
    assert compute_availability(_SurveyStub(starts_at=NOW), 0, NOW)[0] == "open"


def test_availability_ended():
    # 截止时刻本身即算已截止 (ends_at 是开区间上界)
    state, message = compute_availability(_SurveyStub(ends_at=NOW), 0, NOW)
    assert state == "ended"
    assert message == AVAILABILITY_MESSAGES["ended"]
    assert compute_availability(_SurveyStub(ends_at=NOW + timedelta(seconds=1)), 0, NOW)[0] == "open"


def test_availability_full():
    survey = _SurveyStub(max_submissions=10)
    state, message = compute_availability(survey, 10, NOW)
    assert state == "full"
    assert message == AVAILABILITY_MESSAGES["full"]
    assert compute_availability(survey, 9, NOW)[0] == "open"
    # 并发穿透导致超额时同样算满
    assert compute_availability(survey, 11, NOW)[0] == "full"


def test_availability_priority_inactive_over_ended():
    # 停用是管理员的显式意图, 必须压过时间窗, 否则面板上看到的是"已截止"而不是"已停用"
    survey = _SurveyStub(is_active=False, status="draft", ends_at=NOW - timedelta(days=1), max_submissions=1)
    assert compute_availability(survey, 99, NOW)[0] == "inactive"


def test_availability_priority_not_started_over_ended_and_full():
    # 起止时间被配反 (starts_at 在 ends_at 之后) 时以"未开始"为准, 提示才对得上运营的下一步动作
    survey = _SurveyStub(
        starts_at=NOW + timedelta(days=1), ends_at=NOW - timedelta(days=1), max_submissions=1
    )
    assert compute_availability(survey, 99, NOW)[0] == "not_started"


def test_closed_message_overrides_default_text():
    survey = _SurveyStub(ends_at=NOW - timedelta(days=1), closed_message="报名已结束, 下期再来")
    assert compute_availability(survey, 0, NOW) == ("ended", "报名已结束, 下期再来")

    # 各状态都吃这条自定义文案, 不是只有 ended 特殊
    assert compute_availability(
        _SurveyStub(is_active=False, closed_message="报名已结束, 下期再来"), 0, NOW
    ) == ("inactive", "报名已结束, 下期再来")

    # open 时不带文案, 否则前端会把运营文案当禁填提示渲染出来
    assert compute_availability(_SurveyStub(closed_message="报名已结束, 下期再来"), 0, NOW) == ("open", None)


def test_availability_accepts_timezone_aware_inputs():
    # 面板传来的时间带 tz, 库里取回的是 naive: 不归一化会直接抛 offset-naive/aware 比较异常
    survey = _SurveyStub(ends_at=datetime(2026, 8, 6, 11, 0, tzinfo=timezone.utc))
    assert compute_availability(survey, 0, NOW)[0] == "ended"
    # now 带 tz 也要能比
    assert compute_availability(
        _SurveyStub(starts_at=datetime(2026, 8, 6, 20, 1, tzinfo=timezone(timedelta(hours=8)))),
        0,
        datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
    )[0] == "not_started"


# ==================== _as_naive_utc ====================

def test_as_naive_utc_normalizes_aware_and_passes_naive():
    aware = datetime(2026, 8, 6, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    assert _as_naive_utc(aware) == datetime(2026, 8, 6, 12, 0)
    assert _as_naive_utc(aware).tzinfo is None

    naive = datetime(2026, 8, 6, 12, 0)
    assert _as_naive_utc(naive) is naive
    assert _as_naive_utc(None) is None

    # 归一化后两种输入都能直接和 naive now 比较, 不抛 TypeError
    assert _as_naive_utc(aware) <= NOW
    assert _as_naive_utc(naive) <= NOW


# ==================== 访问口令 ====================

def test_access_password_hash_roundtrip():
    stored = hash_access_password("Kivotos-2026")
    assert stored.startswith("pbkdf2_sha256$200000$")
    assert "Kivotos-2026" not in stored  # 明文绝不能出现在入库串里

    assert verify_access_password("Kivotos-2026", stored) is True
    assert verify_access_password("kivotos-2026", stored) is False
    assert verify_access_password("Kivotos-2027", stored) is False
    assert verify_access_password("", stored) is False


def test_access_password_hash_is_salted():
    # 同一口令两次哈希必须不同, 否则一次撞库就能横扫所有用同口令的卷
    assert hash_access_password("same") != hash_access_password("same")


def test_verify_passes_when_no_password_configured():
    assert verify_access_password("whatever", None) is True
    assert verify_access_password("whatever", "") is True
    assert verify_access_password(None, None) is True


def test_verify_fails_closed_on_corrupted_hash():
    # 入库串被写坏时必须验不过, 不能因为解析失败反而放行
    assert verify_access_password("x", "plaintext") is False
    assert verify_access_password("x", "pbkdf2_sha256$notanumber$aa$bb") is False
    assert verify_access_password("x", "pbkdf2_sha256$200000$zz$bb") is False
    assert verify_access_password("x", "pbkdf2_sha256$0$aa$bb") is False
    assert verify_access_password("x", "md5$200000$aa$bb") is False


# ==================== 访问口令 (线程池版, 供 async 端点使用) ====================

async def test_access_password_async_matches_sync_semantics():
    """await 版与同步版必须逐条等价, 否则端点换用异步版就是在偷偷改口令闸门的判定。"""
    stored = await hash_access_password_async("Kivotos-2026")
    assert stored.startswith("pbkdf2_sha256$200000$")
    assert "Kivotos-2026" not in stored
    # 同步版能验通过异步版产出的串, 反之亦然 (两版共用同一套格式与轮数)
    assert verify_access_password("Kivotos-2026", stored) is True
    assert await verify_access_password_async("Kivotos-2026", hash_access_password("Kivotos-2026")) is True

    assert await verify_access_password_async("kivotos-2026", stored) is False
    assert await verify_access_password_async("Kivotos-2027", stored) is False
    assert await verify_access_password_async("", stored) is False
    # 无口令放行 / 坏串 fail-closed 两条边界同样对齐
    assert await verify_access_password_async("whatever", None) is True
    assert await verify_access_password_async("x", "plaintext") is False


async def test_access_password_async_runs_off_the_event_loop_thread(monkeypatch):
    """pbkdf2 必须落在别的线程上跑。

    改回事件循环线程上同步调用时本用例会挂 —— 那正是把整个单进程 API 卡住 60-100ms 的写法,
    而 unlock/submit 是未认证公开路径, 谁都能无限次触发。
    """
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _record_hash(raw):
        seen.append(threading.get_ident())
        return "pbkdf2_sha256$1$00$00"

    def _record_verify(raw, stored):
        seen.append(threading.get_ident())
        return True

    monkeypatch.setattr("app.services.survey.hash_access_password", _record_hash)
    monkeypatch.setattr("app.services.survey.verify_access_password", _record_verify)

    await hash_access_password_async("pw")
    await verify_access_password_async("pw", "stored")

    assert len(seen) == 2
    assert all(tid != loop_thread for tid in seen)


# ==================== create_survey / update_survey ====================

async def test_create_survey_persists_lifecycle_settings(tmp_path):
    """面板创建时填的生命周期/门禁/文案字段必须一并落库 (逐字段手写最容易在这里漏)。"""
    session = await _session(tmp_path)
    survey = await SurveyService.create_survey(
        session,
        SurveyCreate(
            title="活动报名",
            category="collection",
            starts_at=datetime(2026, 9, 1, 0, 0),
            ends_at=datetime(2026, 9, 30, 0, 0),
            max_submissions=100,
            max_submissions_per_ip=2,
            require_consent=True,
            privacy_notice="仅用于活动统计",
            closed_message="报名已结束",
            success_message="报名成功, 等待通知",
            action_webhook=True,
            webhook_url="https://example.com/hook",
            questions=[],
        ),
    )

    assert survey.starts_at == datetime(2026, 9, 1, 0, 0)
    assert survey.ends_at == datetime(2026, 9, 30, 0, 0)
    assert survey.max_submissions == 100
    assert survey.max_submissions_per_ip == 2
    assert survey.require_consent is True
    assert survey.privacy_notice == "仅用于活动统计"
    assert survey.closed_message == "报名已结束"
    assert survey.success_message == "报名成功, 等待通知"
    assert survey.action_webhook is True
    assert survey.webhook_url == "https://example.com/hook"
    # 未提供的动作开关仍按 category 播种 (收集表全关)
    assert survey.review_required is False
    assert survey.action_add_whitelist is False


async def test_create_survey_honors_status(tmp_path):
    """面板新建时能直接选草稿。SurveyCreate 不收 status 的话 pydantic 会静默丢弃,
    表现为"建的时候选了草稿, 建出来却是已发布", 且这种丢弃不会报任何错。"""
    session = await _session(tmp_path)

    draft = await SurveyService.create_survey(
        session, SurveyCreate(title="草稿卷", category="collection", status="draft", questions=[])
    )
    assert draft.status == "draft"

    default = await SurveyService.create_survey(
        session, SurveyCreate(title="默认卷", category="collection", questions=[])
    )
    assert default.status == "published"  # 不传仍走既有默认, 不改变存量行为


async def test_create_survey_normalizes_aware_datetimes(tmp_path):
    # 带 tz 的时间直接入库会被 SQLite 丢掉时区, 变成错的墙钟时间
    session = await _session(tmp_path)
    survey = await SurveyService.create_survey(
        session,
        SurveyCreate(
            title="带时区",
            ends_at=datetime(2026, 9, 30, 20, 0, tzinfo=timezone(timedelta(hours=8))),
            questions=[],
        ),
    )
    assert survey.ends_at == datetime(2026, 9, 30, 12, 0)
    assert survey.ends_at.tzinfo is None


async def test_update_survey_sets_clears_and_keeps_access_password(tmp_path):
    session = await _session(tmp_path)
    survey = await SurveyService.create_survey(session, SurveyCreate(title="口令卷", questions=[]))
    assert survey.access_password_hash is None

    # 非空串 -> 设为新口令 (明文不落库)
    await SurveyService.update_survey(session, survey, SurveyUpdate(access_password="letmein"))
    stored = survey.access_password_hash
    assert stored is not None and "letmein" not in stored
    assert verify_access_password("letmein", stored) is True
    # access_password 不是列, 绝不能被 setattr 到 Survey 上
    assert not hasattr(survey, "access_password")

    # 不传该键 -> 保持原口令 (面板改别的设置时不必回填口令)
    await SurveyService.update_survey(session, survey, SurveyUpdate(title="口令卷改名"))
    assert survey.access_password_hash == stored

    # 显式空串 -> 清除口令
    await SurveyService.update_survey(session, survey, SurveyUpdate(access_password=""))
    assert survey.access_password_hash is None


# ==================== count_submissions_by_ip ====================

async def test_count_submissions_by_ip(tmp_path):
    session = await _session(tmp_path)
    survey = await SurveyService.create_survey(session, SurveyCreate(title="限流卷", questions=[]))
    other = await SurveyService.create_survey(session, SurveyCreate(title="别的卷", questions=[]))
    session.add_all([
        Submission(survey_id=survey.id, ip_address="1.2.3.4"),
        Submission(survey_id=survey.id, ip_address="1.2.3.4"),
        Submission(survey_id=survey.id, ip_address="5.6.7.8"),
        Submission(survey_id=other.id, ip_address="1.2.3.4"),  # 别的卷不计入
    ])
    await session.commit()

    assert await SurveyService.count_submissions_by_ip(session, survey.id, "1.2.3.4") == 2
    assert await SurveyService.count_submissions_by_ip(session, survey.id, "5.6.7.8") == 1
    assert await SurveyService.count_submissions_by_ip(session, survey.id, "9.9.9.9") == 0
    # 取不到真实 IP 时返回 0 = 不限, 不能把所有人挡在门外
    assert await SurveyService.count_submissions_by_ip(session, survey.id, None) == 0
    assert await SurveyService.count_submissions_by_ip(session, survey.id, "") == 0


# ==================== 审核队列过滤口径 (get_submissions) ====================

async def _seed_review_queue(session):
    """建三张卷: 白名单卷(需审)、开了人工审核的收集表、免审收集表, 各一条提交。"""
    wl = await SurveyService.create_survey(
        session, SurveyCreate(title="白名单卷", category="whitelist", questions=[])
    )
    col_review = await SurveyService.create_survey(
        session,
        SurveyCreate(title="要审的收集表", category="collection", review_required=True, questions=[]),
    )
    col_free = await SurveyService.create_survey(
        session, SurveyCreate(title="免审收集表", category="collection", questions=[])
    )
    # 前提: 三张卷的 review_required 确实是 True/True/False, 否则下面的断言证明不了任何事
    assert (wl.review_required, col_review.review_required, col_free.review_required) == (True, True, False)

    session.add_all([
        Submission(survey_id=wl.id, player_name="Steve", status="pending"),
        Submission(survey_id=col_review.id, player_name="Alex", status="pending"),
        Submission(survey_id=col_free.id, player_name="Herobrine", status="approved"),
    ])
    await session.commit()
    return wl, col_review, col_free


async def test_review_required_filter_covers_reviewing_collection_surveys(tmp_path):
    """审核队列按 review_required 取数才能捞到"开了人工审核的收集表"。

    按 category='whitelist' 取数时这类提交在面板里没有任何入口, 玩家永久停在"等待审核"。
    """
    session = await _session(tmp_path)
    await _seed_review_queue(session)

    subs, total = await SubmissionService.get_submissions(session, review_required=True)
    assert total == 2
    assert len(subs) == 2  # total 与列表口径必须一致 (count_query 漏 join 时这里会对不上)
    assert {s.player_name for s in subs} == {"Steve", "Alex"}

    # 旧口径: 只看得到白名单卷, 要审的收集表被挡在外面
    subs, total = await SubmissionService.get_submissions(session, category="whitelist")
    assert total == 1
    assert {s.player_name for s in subs} == {"Steve"}

    # 免审卷的提交在两种口径下都不该进审核队列
    subs, total = await SubmissionService.get_submissions(session, review_required=False)
    assert total == 1
    assert {s.player_name for s in subs} == {"Herobrine"}


async def test_review_required_filter_stacks_with_category_and_status(tmp_path):
    """两个过滤可叠加 (共用同一次联表, 重复 join 会在这里直接抛异常)。"""
    session = await _session(tmp_path)
    await _seed_review_queue(session)

    subs, total = await SubmissionService.get_submissions(
        session, category="collection", review_required=True
    )
    assert total == 1
    assert [s.player_name for s in subs] == ["Alex"]

    # 统计口径 (stats/overview 走的就是 size=1 只取 total 这条路)
    _, pending = await SubmissionService.get_submissions(session, 1, 1, "pending", review_required=True)
    assert pending == 2
    _, approved = await SubmissionService.get_submissions(session, 1, 1, "approved", review_required=True)
    assert approved == 0

    # 不传任何过滤 = 全量, 不受新参数影响
    _, total_all = await SubmissionService.get_submissions(session)
    assert total_all == 3


async def test_review_required_filter_preserves_legacy_behavior(tmp_path):
    """存量现网数据 (白名单卷需审 + 收集表免审) 下, 新旧两种口径取到的集合必须完全一致。

    现网 156 条提交全在这个形态, 切换过滤口径不允许让面板上的审核队列发生任何变化。
    """
    session = await _session(tmp_path)
    wl = await SurveyService.create_survey(
        session, SurveyCreate(title="现网白名单卷", category="whitelist", questions=[])
    )
    col = await SurveyService.create_survey(
        session, SurveyCreate(title="现网收集表", category="collection", questions=[])
    )
    assert (wl.review_required, col.review_required) == (True, False)

    session.add_all([
        Submission(survey_id=wl.id, player_name="P1", status="pending"),
        Submission(survey_id=wl.id, player_name="P2", status="approved"),
        Submission(survey_id=wl.id, player_name="P3", status="rejected"),
        Submission(survey_id=col.id, player_name="C1", status="approved"),
        Submission(survey_id=col.id, player_name="C2", status="approved"),
    ])
    await session.commit()

    for status in (None, "pending", "approved", "rejected"):
        by_category, total_category = await SubmissionService.get_submissions(
            session, status=status, category="whitelist"
        )
        by_flag, total_flag = await SubmissionService.get_submissions(
            session, status=status, review_required=True
        )
        assert total_category == total_flag
        assert [s.id for s in by_category] == [s.id for s in by_flag]

    # 反向: 收集表结果页的口径同样不变
    _, total_collection = await SubmissionService.get_submissions(session, category="collection")
    assert total_collection == 2


# ==================== duplicate_survey ====================

async def test_duplicate_survey_creates_inactive_draft_copy(tmp_path):
    session = await _session(tmp_path)
    survey_id, _ = await _seed_branching_survey(session)

    clone = await SurveyService.duplicate_survey(session, survey_id, created_by=42)
    source = await SurveyService.get_survey_by_id(session, survey_id)

    assert clone.id != source.id
    assert clone.code != source.code
    assert clone.title == "原卷 (副本)"
    assert clone.is_active is False
    assert clone.status == "draft"
    assert clone.created_by == 42
    assert clone.category == source.category  # 其余配置照抄

    assert len(clone.questions) == len(source.questions) == 3
    assert [q.title for q in sorted(clone.questions, key=lambda q: q.order)] == \
           [q.title for q in sorted(source.questions, key=lambda q: q.order)]

    # 原卷不受影响
    assert source.is_active is True
    assert source.title == "原卷"


async def test_duplicate_survey_does_not_copy_submissions(tmp_path):
    session = await _session(tmp_path)
    survey_id, _ = await _seed_branching_survey(session)
    session.add(Submission(survey_id=survey_id, ip_address="1.2.3.4"))
    await session.commit()

    clone = await SurveyService.duplicate_survey(session, survey_id, created_by=None)
    assert await SurveyService.get_submission_count(session, clone.id) == 0
    assert await SurveyService.get_submission_count(session, survey_id) == 1


async def test_duplicate_survey_remaps_condition_question_ids(tmp_path):
    """副本的分支必须指向副本自己的题 —— 删掉重映射逻辑本用例必须挂。"""
    session = await _session(tmp_path)
    survey_id, source_q1_id = await _seed_branching_survey(session)

    clone = await SurveyService.duplicate_survey(session, survey_id, created_by=None)
    by_title = {q.title: q for q in clone.questions}
    new_q1_id = by_title["玩过吗"].id
    # 前提: 副本的题确实拿到了新 id, 否则下面的断言证明不了任何事
    assert new_q1_id != source_q1_id

    # 新形态: rules[].question_id 指向副本的第一题, 其余规则字段原样保留
    rules = by_title["玩了多久"].condition["rules"]
    assert [r["question_id"] for r in rules] == [new_q1_id]
    assert rules[0]["operator"] == "eq"
    assert rules[0]["value"] == "yes"
    assert by_title["玩了多久"].condition["match"] == "all"

    # 旧形态 (存量库里全是这个) 同样要被重写
    assert by_title["旧形态分支"].condition["depends_on"] == new_q1_id
    assert by_title["旧形态分支"].condition["show_when"] == "yes"

    # 原卷的条件不能被顺手改掉
    source = await SurveyService.get_survey_by_id(session, survey_id)
    src_by_title = {q.title: q for q in source.questions}
    assert src_by_title["玩了多久"].condition["rules"][0]["question_id"] == source_q1_id
    assert src_by_title["旧形态分支"].condition["depends_on"] == source_q1_id


async def test_duplicate_survey_drops_unmappable_condition_rules(tmp_path):
    """映射不到的规则要丢弃, 而不是留一条指向别卷题目的悬空引用。"""
    session = await _session(tmp_path)
    survey = await SurveyService.create_survey(
        session, SurveyCreate(title="悬空", category="collection", questions=[])
    )
    session.add_all([
        Question(
            survey_id=survey.id, title="新形态悬空", type="text", order=0,
            condition={
                "action": "show", "match": "any",
                "rules": [{"question_id": 999999, "operator": "eq", "value": "A"}],
            },
        ),
        Question(
            survey_id=survey.id, title="旧形态悬空", type="text", order=1,
            condition={"depends_on": 999999, "show_when": "A"},
        ),
    ])
    await session.commit()

    clone = await SurveyService.duplicate_survey(session, survey.id, created_by=None)
    by_title = {q.title: q for q in clone.questions}
    assert by_title["新形态悬空"].condition is None
    assert by_title["旧形态悬空"].condition is None


async def test_duplicate_survey_carries_over_question_config(tmp_path):
    """题目的选项/题型/排序等配置必须整套跟着复制, 副本不能只剩个标题。"""
    session = await _session(tmp_path)
    survey_id, _ = await _seed_branching_survey(session)

    clone = await SurveyService.duplicate_survey(session, survey_id, created_by=None)
    source = await SurveyService.get_survey_by_id(session, survey_id)
    new_q = next(q for q in clone.questions if q.title == "玩过吗")
    src_q = next(q for q in source.questions if q.title == "玩过吗")

    assert new_q.survey_id == clone.id  # 归属换成副本自己
    assert new_q.id != src_q.id
    assert new_q.type == src_q.type == "single"
    assert new_q.options == src_q.options == [
        {"value": "yes", "label": "玩过"},
        {"value": "no", "label": "没玩过"},
    ]
    assert new_q.order == src_q.order
    assert new_q.is_required == src_q.is_required


async def test_duplicate_survey_rejects_missing_survey(tmp_path):
    session = await _session(tmp_path)
    with pytest.raises(ValueError):
        await SurveyService.duplicate_survey(session, 999999, created_by=None)
