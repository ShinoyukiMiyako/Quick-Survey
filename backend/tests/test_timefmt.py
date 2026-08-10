"""对外时间序列化的时区契约.

回归的是这个线上现象: 库里存的是 naive UTC, 出站却不带时区标记, 前端 new Date() 按
本地时间解析 -> 面板把 8/10 04:34 (北京) 显示成 8/9 20:34, 差 8 小时还退了一天。
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.public import _submission_status_dict
from app.core.timefmt import iso_utc

CST = timezone(timedelta(hours=8))


def test_naive_datetime_is_marked_as_utc():
    assert iso_utc(datetime(2026, 8, 9, 20, 34, 20)) == "2026-08-09T20:34:20+00:00"


def test_aware_datetime_keeps_its_own_offset():
    aware = datetime(2026, 8, 10, 4, 34, 20, tzinfo=CST)
    assert iso_utc(aware) == "2026-08-10T04:34:20+08:00"


def test_none_passes_through():
    assert iso_utc(None) is None


def test_marked_string_reads_back_as_correct_beijing_time():
    # 库里的 naive UTC 20:34 (8/9) 就是北京时间次日 04:34 —— 客户端按标记解析必须得到这个值
    parsed = datetime.fromisoformat(iso_utc(datetime(2026, 8, 9, 20, 34, 20)))
    assert parsed.astimezone(CST) == datetime(2026, 8, 10, 4, 34, 20, tzinfo=CST)


def test_unmarked_isoformat_would_be_misread_as_local():
    # 反证: 不带标记的串在客户端等价于"本地 20:34", 与真实时刻差整整一个时区偏移
    naive = datetime(2026, 8, 9, 20, 34, 20)
    misread = datetime.fromisoformat(naive.isoformat()).replace(tzinfo=CST)
    correct = datetime.fromisoformat(iso_utc(naive))
    assert misread != correct
    assert correct - misread == timedelta(hours=8)


def test_submission_status_timeline_carries_timezone():
    """玩家端进度时间线: 三个时间字段必须全部带时区标记, 少一个就会显示成过去 8 小时。"""
    sub = SimpleNamespace(
        id=1,
        token="t" * 32,
        player_name="Alice",
        status="approved",
        created_at=datetime(2026, 8, 9, 20, 34, 20),
        first_viewed_at=datetime(2026, 8, 9, 20, 35, 24),
        reviewed_at=datetime(2026, 8, 9, 21, 0, 0),
        code_issued_at=None,
        fill_duration=3780.0,
        review_note=None,
        survey=SimpleNamespace(title="进服申请"),
    )

    timeline = _submission_status_dict(sub)["timeline"]

    assert timeline["submitted_at"] == "2026-08-09T20:34:20+00:00"
    assert timeline["first_viewed_at"] == "2026-08-09T20:35:24+00:00"
    assert timeline["reviewed_at"] == "2026-08-09T21:00:00+00:00"


def test_submission_status_timeline_keeps_none_for_missing_steps():
    sub = SimpleNamespace(
        id=2,
        token="u" * 32,
        player_name="Bob",
        status="pending",
        created_at=datetime(2026, 8, 9, 20, 34, 20),
        first_viewed_at=None,
        reviewed_at=None,
        code_issued_at=None,
        fill_duration=None,
        review_note=None,
        survey=None,
    )

    timeline = _submission_status_dict(sub)["timeline"]

    assert timeline["submitted_at"].endswith("+00:00")
    assert timeline["first_viewed_at"] is None
    assert timeline["reviewed_at"] is None
