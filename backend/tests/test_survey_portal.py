"""门户可选列表 + 批量重排的核心逻辑测试。

覆盖 阶段1 的多表单门户: list_public_surveys 的可见性过滤/栏目过滤/排序,
以及 reorder_surveys 的批量落库。删掉对应逻辑这些断言必须挂掉。
"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.db import Base
from app.models import Survey
from app.services.survey import SurveyService
from app.schemas import SurveyReorderItem


async def _make_session(tmp_path) -> AsyncSession:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


async def _add(session, **kw) -> Survey:
    s = Survey(**kw)
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


async def test_list_public_surveys_visibility_filters(tmp_path):
    session = await _make_session(tmp_path)
    # 出现: 启用 + published + public
    await _add(session, title="可选", code="ok1", is_active=True, status="published", visibility="public")
    # 不出现: 停用 / 草稿 / 非公开
    await _add(session, title="停用", code="x1", is_active=False, status="published", visibility="public")
    await _add(session, title="草稿", code="x2", is_active=True, status="draft", visibility="public")
    await _add(session, title="不公开", code="x3", is_active=True, status="published", visibility="unlisted")

    result = await SurveyService.list_public_surveys(session)
    assert [s.code for s in result] == ["ok1"]


async def test_list_public_surveys_category_filter(tmp_path):
    session = await _make_session(tmp_path)
    await _add(session, title="白名单卷", code="wl", is_active=True, status="published", visibility="public", category="whitelist")
    await _add(session, title="收集表", code="col", is_active=True, status="published", visibility="public", category="collection")

    wl = await SurveyService.list_public_surveys(session, category="whitelist")
    assert [s.code for s in wl] == ["wl"]
    col = await SurveyService.list_public_surveys(session, category="collection")
    assert [s.code for s in col] == ["col"]


async def test_list_public_surveys_ordering_pinned_then_sort_order(tmp_path):
    session = await _make_session(tmp_path)
    await _add(session, title="A", code="a", is_active=True, status="published", visibility="public", is_pinned=False, sort_order=2)
    await _add(session, title="B", code="b", is_active=True, status="published", visibility="public", is_pinned=True, sort_order=5)
    await _add(session, title="C", code="c", is_active=True, status="published", visibility="public", is_pinned=False, sort_order=1)

    result = await SurveyService.list_public_surveys(session)
    # 置顶 b 最前; 其余按 sort_order 升序: c(1) 在 a(2) 前
    assert [s.code for s in result] == ["b", "c", "a"]


async def test_reorder_surveys_updates_and_skips_missing(tmp_path):
    session = await _make_session(tmp_path)
    a = await _add(session, title="A", code="a", is_active=True, sort_order=0)
    b = await _add(session, title="B", code="b", is_active=True, sort_order=1)

    updated = await SurveyService.reorder_surveys(
        session,
        [
            SurveyReorderItem(id=a.id, sort_order=10),
            SurveyReorderItem(id=b.id, sort_order=3),
            SurveyReorderItem(id=999999, sort_order=0),  # 不存在, 跳过
        ],
    )
    assert updated == 2
    await session.refresh(a)
    await session.refresh(b)
    assert a.sort_order == 10
    assert b.sort_order == 3
