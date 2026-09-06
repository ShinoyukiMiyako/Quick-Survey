"""
定时清理服务的回归测试。

核心契约（业务决策: 留答案、清图片）:
- 已审核提交的答案文本行必须保留, 仅清空其图片引用并删除磁盘图片;
- 关联 uploaded_files 记录及磁盘文件删除;
- 未审核(pending)提交完全不受影响;
- 对同一已处理提交重复执行幂等(不重复计数、答案仍在)。
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.db import Base
from app.models import Survey, Question, Submission, Answer, UploadedFile
from app.core.config import Settings, UploadSettings
from app.services.cleanup import CleanupService


async def _make_session(tmp_path) -> AsyncSession:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


def _patch_upload_dir(monkeypatch, upload_dir):
    fake = Settings(upload=UploadSettings(path=str(upload_dir)))
    monkeypatch.setattr("app.services.cleanup.get_settings", lambda: fake)


async def _seed_reviewed_submission(session, upload_dir):
    """建一份已审核提交: 1 个图片题(两张图) + 1 个文本题, 外加一条 uploaded_files 记录。"""
    survey = Survey(title="测试问卷", code="cleanupcode", is_active=True)
    session.add(survey)
    await session.commit()
    await session.refresh(survey)

    q_img = Question(survey_id=survey.id, title="上传图", type="image", order=1)
    q_txt = Question(survey_id=survey.id, title="留言", type="text", order=2)
    session.add_all([q_img, q_txt])
    await session.commit()
    await session.refresh(q_img)
    await session.refresh(q_txt)

    sub = Submission(
        survey_id=survey.id,
        player_name="Alice",
        status="approved",
        reviewed_at=datetime.now(timezone.utc),
    )
    session.add(sub)
    await session.commit()
    await session.refresh(sub)

    (upload_dir / "a.jpg").write_bytes(b"aaa")
    (upload_dir / "b.jpg").write_bytes(b"bbbb")

    ans_img = Answer(
        submission_id=sub.id,
        question_id=q_img.id,
        content={"images": ["/uploads/a.jpg", "/uploads/b.jpg"]},
    )
    ans_txt = Answer(
        submission_id=sub.id,
        question_id=q_txt.id,
        content={"text": "保留我"},
    )
    session.add_all([ans_img, ans_txt])

    uf = UploadedFile(
        filename="orig.jpg",
        stored_name="a.jpg",
        file_path=str(upload_dir / "a.jpg"),
        file_size=3,
        mime_type="image/jpeg",
        submission_id=sub.id,
    )
    session.add(uf)
    await session.commit()
    return sub, ans_img, ans_txt


async def test_cleanup_keeps_answers_clears_images(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    _patch_upload_dir(monkeypatch, upload_dir)

    session = await _make_session(tmp_path)
    sub, ans_img, ans_txt = await _seed_reviewed_submission(session, upload_dir)

    stats = await CleanupService.cleanup_reviewed_submissions(session)

    # 图片磁盘文件被删
    assert not (upload_dir / "a.jpg").exists()
    assert not (upload_dir / "b.jpg").exists()

    # 答案行全部保留 (核心: 留答案)
    remaining = (await session.execute(select(Answer))).scalars().all()
    assert len(remaining) == 2

    # 图片引用被清空, 文本内容原样保留
    await session.refresh(ans_img)
    await session.refresh(ans_txt)
    assert ans_img.content == {"images": []}
    assert ans_txt.content == {"text": "保留我"}

    # 提交元数据保留
    await session.refresh(sub)
    assert sub.status == "approved"

    # uploaded_files 记录被删
    ufs = (await session.execute(select(UploadedFile))).scalars().all()
    assert len(ufs) == 0

    # 统计断言具体业务结果
    assert stats["images_cleared"] == 2
    assert stats["submissions_cleaned"] == 1
    assert stats["files_deleted"] == 2  # a.jpg + b.jpg (uploaded_files 的 a.jpg 已被前一步删, 不重复计)
    assert stats["bytes_freed"] == 7  # 3 + 4

    await session.close()


async def test_cleanup_is_idempotent_on_processed_submission(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    _patch_upload_dir(monkeypatch, upload_dir)

    session = await _make_session(tmp_path)
    await _seed_reviewed_submission(session, upload_dir)

    await CleanupService.cleanup_reviewed_submissions(session)
    stats2 = await CleanupService.cleanup_reviewed_submissions(session)

    # 第二次无图可清, 不再计数, 答案仍在
    assert stats2["images_cleared"] == 0
    assert stats2["submissions_cleaned"] == 0
    assert stats2["files_deleted"] == 0
    assert len((await session.execute(select(Answer))).scalars().all()) == 2

    await session.close()


async def test_cleanup_skips_pending_submission(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    _patch_upload_dir(monkeypatch, upload_dir)

    session = await _make_session(tmp_path)
    survey = Survey(title="测试问卷", code="pendingcode", is_active=True)
    session.add(survey)
    await session.commit()
    await session.refresh(survey)

    q_img = Question(survey_id=survey.id, title="上传图", type="image", order=1)
    session.add(q_img)
    await session.commit()
    await session.refresh(q_img)

    sub = Submission(survey_id=survey.id, player_name="Bob", status="pending")
    session.add(sub)
    await session.commit()
    await session.refresh(sub)

    (upload_dir / "keep.jpg").write_bytes(b"keepme")
    ans = Answer(
        submission_id=sub.id,
        question_id=q_img.id,
        content={"images": ["/uploads/keep.jpg"]},
    )
    session.add(ans)
    await session.commit()

    stats = await CleanupService.cleanup_reviewed_submissions(session)

    # 未审核提交完全不动
    assert (upload_dir / "keep.jpg").exists()
    await session.refresh(ans)
    assert ans.content == {"images": ["/uploads/keep.jpg"]}
    assert stats["submissions_cleaned"] == 0
    assert stats["images_cleared"] == 0

    await session.close()


async def test_cleanup_clears_file_attachments(tmp_path, monkeypatch):
    """文件题附件与图片同样要清: 模型包动辄几十 MB, 漏掉它清理任务等于白跑。"""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    _patch_upload_dir(monkeypatch, upload_dir)

    session = await _make_session(tmp_path)

    survey = Survey(title="模型征集", code="filecleanup", is_active=True)
    session.add(survey)
    await session.commit()
    await session.refresh(survey)

    q_file = Question(survey_id=survey.id, title="上传模型", type="file", order=1)
    session.add(q_file)
    await session.commit()
    await session.refresh(q_file)

    sub = Submission(
        survey_id=survey.id,
        player_name="Bob",
        status="rejected",
        reviewed_at=datetime.now(timezone.utc),
    )
    session.add(sub)
    await session.commit()
    await session.refresh(sub)

    (upload_dir / "model.ysm").write_bytes(b"x" * 16)
    ans_file = Answer(
        submission_id=sub.id,
        question_id=q_file.id,
        content={
            "files": [
                {"url": "/uploads/model.ysm", "name": "纸板狐.ysm", "size": 16},
                # 形态不合法的条目不该让清理炸掉, 也不该被拿去拼路径
                {"url": "/uploads/../escape"},
            ]
        },
    )
    session.add(ans_file)
    await session.commit()

    stats = await CleanupService.cleanup_reviewed_submissions(session)

    assert not (upload_dir / "model.ysm").exists()
    await session.refresh(ans_file)
    assert ans_file.content == {"files": []}
    assert stats["files_cleared"] == 2
    assert stats["files_deleted"] == 1
    assert stats["submissions_cleaned"] == 1
