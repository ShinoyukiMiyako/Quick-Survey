"""问卷统计聚合的单元测试: 分布/百分比、数字极值均值、文本样例、状态计数、每日趋势。"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.db import Base
from app.models import Survey, Question, Submission, Answer
from app.services.analytics import build_survey_analytics


async def _session(tmp_path) -> AsyncSession:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analytics.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


async def _make_survey(session: AsyncSession, title: str, code: str) -> Survey:
    survey = Survey(title=title, code=code, category="collection")
    session.add(survey)
    await session.commit()
    await session.refresh(survey)
    return survey


async def _add_questions(session: AsyncSession, survey_id: int, specs: list) -> list[Question]:
    """specs: [(title, type, options)], order 按传入顺序。"""
    questions = [
        Question(survey_id=survey_id, title=title, type=qtype, options=options, order=i)
        for i, (title, qtype, options) in enumerate(specs)
    ]
    session.add_all(questions)
    await session.commit()
    for q in questions:
        await session.refresh(q)
    return questions


async def _add_submission(
    session: AsyncSession, survey_id: int, *, status, created_at, fill_duration, answers: dict
) -> Submission:
    submission = Submission(
        survey_id=survey_id,
        status=status,
        created_at=created_at,
        fill_duration=fill_duration,
    )
    for question_id, content in answers.items():
        submission.answers.append(Answer(question_id=question_id, content=content))
    session.add(submission)
    await session.commit()
    await session.refresh(submission)
    return submission


async def test_build_survey_analytics_full_report(tmp_path):
    session = await _session(tmp_path)
    survey = await _make_survey(session, "综合问卷", "ana00001")
    q_single, q_multi, q_num, q_rating, q_text = await _add_questions(session, survey.id, [
        ("最喜欢的服务器", "single", [
            {"value": "A", "label": "选项A"},
            {"value": "B", "label": "选项B"},
            {"value": "C", "label": "选项C"},
        ]),
        ("玩过的玩法", "multiple", [
            {"value": "X", "label": "生存"},
            {"value": "Y", "label": "建筑"},
        ]),
        ("每周游戏时长", "number", None),
        ("整体评分", "rating", None),
        ("建议", "text", None),
    ])

    now = datetime.now(timezone.utc)
    # 前两条用 naive UTC (SQLite 取回的形态), 后两条用 aware (同会话内新建对象的形态),
    # 两种混用才能压到日期归一化的两条分支
    two_days_ago = (now - timedelta(days=2)).replace(tzinfo=None)
    one_day_ago = now - timedelta(days=1)
    forty_days_ago = now - timedelta(days=40)

    await _add_submission(
        session, survey.id, status="approved", created_at=two_days_ago, fill_duration=100.0,
        answers={
            q_single.id: {"value": "A"},
            q_multi.id: {"values": ["X", "Y"]},
            q_num.id: {"value": 1.0},
            q_rating.id: {"value": 5},
            q_text.id: {"text": "很好用"},
        },
    )
    await _add_submission(
        session, survey.id, status="approved", created_at=two_days_ago, fill_duration=200.0,
        answers={
            q_single.id: {"value": "A"},
            q_multi.id: {"values": ["X"]},
            q_num.id: {"value": 4.5},
            q_rating.id: {"value": 3},
            q_text.id: {"text": "长" * 100},
        },
    )
    await _add_submission(
        session, survey.id, status="rejected", created_at=one_day_ago, fill_duration=None,
        answers={
            q_single.id: {"value": "B"},
            q_multi.id: {"values": []},   # 空数组 = 未作答
            q_num.id: {"value": 7},
            q_text.id: {"text": ""},      # 空串 = 未作答
        },
    )
    # 40 天前的老提交: 计入总量与逐题统计, 但不进 30 天趋势窗口
    await _add_submission(
        session, survey.id, status="pending", created_at=forty_days_ago, fill_duration=None,
        answers={
            q_multi.id: {"values": ["Y"]},
            q_rating.id: {"value": 1},
            q_text.id: {"text": "旧提交"},
        },
    )

    report = await build_survey_analytics(session, survey)

    assert report["survey_id"] == survey.id
    assert report["title"] == "综合问卷"
    assert report["total_submissions"] == 4
    assert report["by_status"] == {"pending": 1, "approved": 2, "rejected": 1}
    # 只对填了耗时的两条求均值, 分母不能是 4
    assert report["avg_fill_duration"] == 150.0
    assert report["daily"] == [
        {"date": (now - timedelta(days=2)).date().isoformat(), "count": 2},
        {"date": (now - timedelta(days=1)).date().isoformat(), "count": 1},
    ]

    rows = report["questions"]
    assert [r["question_id"] for r in rows] == [
        q_single.id, q_multi.id, q_num.id, q_rating.id, q_text.id
    ]
    by_id = {r["question_id"]: r for r in rows}

    single = by_id[q_single.id]
    assert single["title"] == "最喜欢的服务器" and single["type"] == "single"
    assert single["answered"] == 3
    assert single["distribution"] == [
        {"value": "A", "label": "选项A", "count": 2, "percent": 66.7},
        {"value": "B", "label": "选项B", "count": 1, "percent": 33.3},
        {"value": "C", "label": "选项C", "count": 0, "percent": 0.0},
    ]
    assert single["numeric"] is None
    assert single["samples"] == []

    multi = by_id[q_multi.id]
    assert multi["answered"] == 3  # 空数组那条不算作答
    assert multi["distribution"] == [
        {"value": "X", "label": "生存", "count": 2, "percent": 66.7},
        {"value": "Y", "label": "建筑", "count": 2, "percent": 66.7},
    ]

    number = by_id[q_num.id]
    assert number["answered"] == 3
    assert number["numeric"] == {"min": 1.0, "max": 7.0, "avg": 4.2}  # (1+4.5+7)/3 四舍五入到 1 位
    assert number["distribution"] == []
    assert number["samples"] == []

    rating = by_id[q_rating.id]
    assert rating["answered"] == 3
    assert rating["numeric"] == {"min": 1.0, "max": 5.0, "avg": 3.0}

    text = by_id[q_text.id]
    assert text["answered"] == 3  # 空串那条不算作答
    # 最近优先, 每条截断到 80 字
    assert text["samples"] == ["长" * 80, "很好用", "旧提交"]
    assert len(text["samples"][0]) == 80
    assert text["distribution"] == [] and text["numeric"] is None

    await session.close()


async def test_text_samples_keep_only_latest_five(tmp_path):
    session = await _session(tmp_path)
    survey = await _make_survey(session, "留言板", "ana00002")
    (q_text,) = await _add_questions(session, survey.id, [("留言", "short_text", None)])

    now = datetime.now(timezone.utc)
    for i in range(1, 8):
        await _add_submission(
            session, survey.id, status="approved",
            created_at=now - timedelta(hours=8 - i), fill_duration=None,
            answers={q_text.id: {"text": f"回答{i}"}},
        )

    report = await build_survey_analytics(session, survey)
    row = report["questions"][0]

    assert row["answered"] == 7
    assert row["samples"] == ["回答7", "回答6", "回答5", "回答4", "回答3"]
    await session.close()


async def test_empty_survey_report_is_all_zero(tmp_path):
    session = await _session(tmp_path)
    survey = await _make_survey(session, "空卷", "ana00003")
    await _add_questions(session, survey.id, [
        ("是否参加", "boolean", None),
        ("补充说明", "text", None),
    ])

    report = await build_survey_analytics(session, survey)

    assert report["total_submissions"] == 0
    assert report["by_status"] == {"pending": 0, "approved": 0, "rejected": 0}
    assert report["avg_fill_duration"] is None
    assert report["daily"] == []

    boolean_row, text_row = report["questions"]
    assert boolean_row["answered"] == 0
    # 判断题没有 options, 分布仍固定给出是/否两档; answered=0 时 percent 记 0
    assert boolean_row["distribution"] == [
        {"value": "true", "label": "是", "count": 0, "percent": 0.0},
        {"value": "false", "label": "否", "count": 0, "percent": 0.0},
    ]
    assert text_row["samples"] == [] and text_row["numeric"] is None
    await session.close()
