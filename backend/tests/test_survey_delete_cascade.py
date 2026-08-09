"""删除问卷的级联清理测试。

SQLite 默认不强制外键 (连接只设了 journal_mode 与 busy_timeout), 列上的 ondelete=CASCADE
不生效, 真正清理子表的只有 SQLAlchemy relationship 上的 cascade。所以每张挂在问卷下的表
都得有对应的 relationship, 漏一张就留一堆孤儿。删掉 Submission.notifications 的 cascade,
本文件的断言必须挂掉。
"""
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.db import Base
from app.models import Survey, Question, Submission, Answer, BotNotification
from app.services.survey import SurveyService


async def _seed(tmp_path):
    """建两张卷: 一张待删(含题目/提交/答案/通知), 一张保留(同样有提交与通知)。"""
    url = f"sqlite+aiosqlite:///{tmp_path / 'cascade.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as s:
        doomed = Survey(title="待删收集表", code="del01", category="collection")
        s.add(doomed)
        await s.flush()
        q = Question(survey_id=doomed.id, title="Q1", type="short_text", order=0)
        s.add(q)
        await s.flush()
        sub = Submission(survey_id=doomed.id, status="approved")
        sub.answers.append(Answer(question_id=q.id, content={"text": "hi"}))
        s.add(sub)
        await s.flush()
        s.add(BotNotification(submission_id=sub.id, qq="123456", type="submit"))
        s.add(BotNotification(submission_id=sub.id, qq="123456", type="approved"))

        keep = Survey(title="保留卷", code="keep1", category="whitelist")
        s.add(keep)
        await s.flush()
        keep_sub = Submission(survey_id=keep.id, status="pending")
        s.add(keep_sub)
        await s.flush()
        s.add(BotNotification(submission_id=keep_sub.id, qq="999", type="submit"))
        await s.commit()
        ids = (doomed.id, keep.id)

    return engine, session_maker, ids


async def test_delete_survey_leaves_no_orphans(tmp_path):
    engine, session_maker, (doomed_id, keep_id) = await _seed(tmp_path)

    async with session_maker() as s:
        survey = await SurveyService.get_survey_by_id(s, doomed_id)
        await SurveyService.delete_survey(s, survey)
    await engine.dispose()

    # 用同步连接直查, 绕开 ORM 的身份映射, 看库里真实剩下什么。
    # 用完必须 dispose: 连接被 GC 回收时 SQLAlchemy 会打一条 ERROR 日志, 而它出现的时机
    # 不确定, 会飘到后面某个断言 caplog 的测试里, 造成只在全量跑时才复现的假失败。
    db = create_engine(f"sqlite:///{tmp_path / 'cascade.db'}")
    try:
        with db.connect() as c:
            count = lambda sql: c.execute(text(sql)).scalar()  # noqa: E731

            assert count(f"SELECT COUNT(*) FROM surveys WHERE id={doomed_id}") == 0
            assert count(f"SELECT COUNT(*) FROM questions WHERE survey_id={doomed_id}") == 0
            assert count(f"SELECT COUNT(*) FROM submissions WHERE survey_id={doomed_id}") == 0
            assert count("SELECT COUNT(*) FROM answers WHERE question_id NOT IN (SELECT id FROM questions)") == 0
            # 通知队列: 留下孤儿会被 list_pending 原样取走, 插件在审核群 @ 人通知一份已不存在的申请
            assert count(
                "SELECT COUNT(*) FROM bot_notifications WHERE submission_id NOT IN (SELECT id FROM submissions)"
            ) == 0

            # 只删目标卷: 另一张卷的提交与通知必须原样保留
            assert count(f"SELECT COUNT(*) FROM surveys WHERE id={keep_id}") == 1
            assert count(f"SELECT COUNT(*) FROM submissions WHERE survey_id={keep_id}") == 1
            assert count(
                f"SELECT COUNT(*) FROM bot_notifications "
                f"WHERE submission_id IN (SELECT id FROM submissions WHERE survey_id={keep_id})"
            ) == 1
    finally:
        db.dispose()
