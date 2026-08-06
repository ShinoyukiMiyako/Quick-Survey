"""阶段2 场景解耦的核心逻辑测试: 收集表动作默认、免审自动终态、CSV 导出。"""
from types import SimpleNamespace as NS

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.db import Base
from app.models import Question
from app.services.survey import SurveyService, SubmissionService, build_submissions_csv, _answer_to_cell
from app.schemas import SurveyCreate, SubmissionCreate, AnswerSubmit


async def _session(tmp_path) -> AsyncSession:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


async def _add_text_question(session, survey_id) -> Question:
    q = Question(survey_id=survey_id, title="Q", type="text", order=0)
    session.add(q)
    await session.commit()
    await session.refresh(q)
    return q


async def test_create_survey_seeds_collection_actions_off(tmp_path):
    s = await _session(tmp_path)
    col = await SurveyService.create_survey(s, SurveyCreate(title="反馈", category="collection", questions=[]))
    assert col.review_required is False
    assert col.action_add_whitelist is False
    assert col.action_issue_code is False
    assert col.action_notify_group is False

    wl = await SurveyService.create_survey(s, SurveyCreate(title="白名单", category="whitelist", questions=[]))
    assert wl.review_required is True
    assert wl.action_add_whitelist is True
    assert wl.action_issue_code is True
    assert wl.action_notify_group is True


async def test_collection_submission_auto_finalized(tmp_path):
    s = await _session(tmp_path)
    col = await SurveyService.create_survey(s, SurveyCreate(title="投票", category="collection", questions=[]))
    q = await _add_text_question(s, col.id)
    data = SubmissionCreate(answers=[AnswerSubmit(question_id=q.id, content={"text": "hi"})])
    # 匿名(无玩家名)提交, 免审 -> 直接 approved
    sub = await SubmissionService.create_submission(s, col, data, player_name=None)
    assert sub.status == "approved"
    assert sub.player_name is None


async def test_whitelist_submission_stays_pending(tmp_path):
    s = await _session(tmp_path)
    wl = await SurveyService.create_survey(s, SurveyCreate(title="wl", category="whitelist", questions=[]))
    q = await _add_text_question(s, wl.id)
    data = SubmissionCreate(answers=[AnswerSubmit(question_id=q.id, content={"text": "hi"})])
    sub = await SubmissionService.create_submission(s, wl, data, player_name="Alice")
    assert sub.status == "pending"


def test_answer_to_cell_by_type():
    assert _answer_to_cell({"value": True}, "boolean") == "是"
    assert _answer_to_cell({"value": False}, "boolean") == "否"
    assert _answer_to_cell({"values": ["A", "B"]}, "multiple") == "A, B"
    assert _answer_to_cell({"text": "hello"}, "text") == "hello"
    assert _answer_to_cell(None, "text") == ""


def test_build_submissions_csv():
    survey = NS(questions=[
        NS(id=1, title="姓名", type="text", order=0),
        NS(id=2, title="喜欢的颜色", type="multiple", order=1),
    ])
    subs = [
        NS(
            id=5, created_at=None, player_name="Alice", qq="123", status="approved",
            answers=[
                NS(question_id=1, content={"text": "Alice"}),
                NS(question_id=2, content={"values": ["红", "蓝"]}),
            ],
        )
    ]
    csv_text = build_submissions_csv(survey, subs)
    assert csv_text.startswith("﻿")  # Excel BOM
    assert "姓名" in csv_text and "喜欢的颜色" in csv_text
    assert "Alice" in csv_text
    assert "红, 蓝" in csv_text
    assert "approved" in csv_text
