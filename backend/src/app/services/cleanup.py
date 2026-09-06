"""
定时清理服务 - 清理已审核提交的图片文件, 释放磁盘
保留提交记录元数据与答案文本内容（单选/多选/文本/判断）用于日志与审计查询,
仅删除答案引用的图片文件并清空其图片引用; uploaded_files 记录及磁盘文件一并清理。
"""

import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import async_session_maker
from app.models import Submission, Answer, UploadedFile
from app.core.config import get_settings
from app.services.question_types import attachment_urls

logger = logging.getLogger(__name__)


class CleanupService:
    """清理服务"""
    
    _task: Optional[asyncio.Task] = None
    _running: bool = False
    
    @classmethod
    async def cleanup_reviewed_submissions(cls, db: AsyncSession) -> dict:
        """
        清理已审核提交的图片, 保留答案文本记录。

        保留:
        - submissions 表记录（元数据：状态、审核时间、审核备注等）
        - answers 表记录（填写内容：单选/多选/文本/判断），仅清空其中的图片引用

        删除:
        - answers.content.images 指向的 uploads/ 图片文件, 并把该答案的 images 置空
        - uploaded_files 表中的文件记录及其磁盘文件
        """
        settings = get_settings()
        upload_dir = Path(settings.upload.path)

        stats = {
            "submissions_cleaned": 0,
            "images_cleared": 0,
            "files_cleared": 0,
            "files_deleted": 0,
            "bytes_freed": 0,
        }

        # 查找已审核的提交（状态为 approved 或 rejected）
        # 使用 selectinload 预加载 answers 关系，避免异步懒加载问题
        query = select(Submission).options(
            selectinload(Submission.answers)
        ).where(
            and_(
                Submission.status.in_(["approved", "rejected"]),
                Submission.reviewed_at.isnot(None),
            )
        )
        result = await db.execute(query)
        submissions = result.scalars().all()

        for submission in submissions:
            submission_touched = False

            for answer in submission.answers:
                content = answer.content or {}
                # 答案里引用上传文件的有两处: 图片题的 images (字符串数组) 与文件题的 files
                # (对象数组)。附件动辄几十 MB, 只清图片等于清理任务对文件题白跑。
                new_content = None

                for key, stat_key in (("images", "images_cleared"), ("files", "files_cleared")):
                    refs = content.get(key) or []
                    # 已清理过(或该答案不是这个题型)的没有引用, 跳过 -> 任务对已处理提交幂等
                    if not refs:
                        continue

                    for url in attachment_urls(content, key):
                        file_path = upload_dir / url.rsplit("/", 1)[-1]
                        if file_path.exists():
                            try:
                                file_size = file_path.stat().st_size
                                os.remove(file_path)
                                stats["files_deleted"] += 1
                                stats["bytes_freed"] += file_size
                            except Exception:
                                logger.exception("[Cleanup] 删除答案附件失败: %s", file_path)

                    # 清空引用但保留答案行。JSON 列须整体重新赋值, SQLAlchemy 才会标记为脏并落库。
                    new_content = dict(new_content if new_content is not None else content)
                    new_content[key] = []
                    stats[stat_key] += len(refs)

                if new_content is not None:
                    answer.content = new_content
                    submission_touched = True

            if submission_touched:
                stats["submissions_cleaned"] += 1

        # 删除关联的上传文件记录
        file_query = select(UploadedFile).where(
            UploadedFile.submission_id.in_([s.id for s in submissions])
        )
        file_result = await db.execute(file_query)
        uploaded_files = file_result.scalars().all()
        
        for uploaded_file in uploaded_files:
            # 如果文件还存在，也删除
            file_path = Path(uploaded_file.file_path)
            if file_path.exists():
                try:
                    file_size = file_path.stat().st_size
                    os.remove(file_path)
                    stats["files_deleted"] += 1
                    stats["bytes_freed"] += file_size
                except Exception:
                    logger.exception("[Cleanup] 删除已记录上传文件失败: %s", file_path)
            await db.delete(uploaded_file)
        
        await db.commit()
        
        return stats
    
    @classmethod
    async def cleanup_orphan_files(cls) -> dict:
        """
        清理孤立的上传文件（没有关联到任何提交的文件）
        超过配置时间的未关联文件会被删除
        """
        settings = get_settings()
        upload_dir = Path(settings.upload.path)
        
        stats = {
            "orphan_files_deleted": 0,
            "bytes_freed": 0,
        }
        
        if not upload_dir.exists():
            return stats
        
        now = datetime.now(timezone.utc)
        threshold_seconds = settings.cleanup.orphan_file_hours * 60 * 60  # 从配置读取
        
        async with async_session_maker() as db:
            # 获取所有数据库中记录的文件名
            query = select(UploadedFile.stored_name)
            result = await db.execute(query)
            db_filenames = set(row[0] for row in result.fetchall())
            
            # 同时从答案中获取引用的附件 (图片题 images + 文件题 files):
            # 被答案引用的文件哪怕 uploaded_files 里没记录, 也不能当孤儿删掉
            answer_query = select(Answer.content)
            answer_result = await db.execute(answer_query)

            for row in answer_result.fetchall():
                content = row[0] or {}
                for key in ("images", "files"):
                    for url in attachment_urls(content, key):
                        db_filenames.add(url.rsplit("/", 1)[-1])
        
        # 遍历上传目录
        for file_path in upload_dir.iterdir():
            if not file_path.is_file():
                continue
            
            filename = file_path.name
            
            # 如果文件在数据库中有记录，跳过
            if filename in db_filenames:
                continue
            
            # 检查文件年龄
            file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
            age_seconds = (now - file_mtime).total_seconds()
            
            if age_seconds > threshold_seconds:
                try:
                    file_size = file_path.stat().st_size
                    os.remove(file_path)
                    stats["orphan_files_deleted"] += 1
                    stats["bytes_freed"] += file_size
                except Exception as e:
                    print(f"[Cleanup] 删除孤立文件失败: {file_path}, 错误: {e}")
        
        return stats
    
    @classmethod
    async def run_cleanup(cls) -> dict:
        """执行一次完整清理"""
        print(f"[Cleanup] 开始清理任务 - {datetime.now()}")
        
        total_stats = {
            "submissions_cleaned": 0,
            "images_cleared": 0,
            "files_cleared": 0,
            "files_deleted": 0,
            "orphan_files_deleted": 0,
            "bytes_freed": 0,
        }
        
        try:
            # 清理已审核的提交数据
            async with async_session_maker() as db:
                stats = await cls.cleanup_reviewed_submissions(db)
                for key, value in stats.items():
                    total_stats[key] = total_stats.get(key, 0) + value
            
            # 清理孤立文件
            orphan_stats = await cls.cleanup_orphan_files()
            total_stats["orphan_files_deleted"] = orphan_stats["orphan_files_deleted"]
            total_stats["bytes_freed"] += orphan_stats["bytes_freed"]
            
            # 格式化释放的空间
            bytes_freed = total_stats["bytes_freed"]
            if bytes_freed >= 1024 * 1024:
                freed_str = f"{bytes_freed / (1024 * 1024):.2f} MB"
            elif bytes_freed >= 1024:
                freed_str = f"{bytes_freed / 1024:.2f} KB"
            else:
                freed_str = f"{bytes_freed} bytes"
            
            print(f"[Cleanup] 清理完成:")
            print(f"  - 清理提交: {total_stats['submissions_cleaned']}")
            print(f"  - 清空图片引用: {total_stats['images_cleared']}")
            print(f"  - 清空附件引用: {total_stats['files_cleared']}")
            print(f"  - 删除文件: {total_stats['files_deleted']}")
            print(f"  - 孤立文件: {total_stats['orphan_files_deleted']}")
            print(f"  - 释放空间: {freed_str}")
            
        except Exception as e:
            print(f"[Cleanup] 清理任务失败: {e}")
            import traceback
            traceback.print_exc()
        
        return total_stats
    
    @classmethod
    async def _cleanup_loop(cls):
        """后台清理循环 - 根据配置的间隔执行"""
        settings = get_settings()
        
        while cls._running:
            try:
                now = datetime.now()
                run_hour = settings.cleanup.run_hour
                interval_days = settings.cleanup.interval_days
                
                # 计算到下一个执行时间点的秒数
                next_run = now.replace(hour=run_hour, minute=0, second=0, microsecond=0)
                if now.hour >= run_hour:
                    # 如果已经过了今天的执行时间，等到下一个周期
                    from datetime import timedelta
                    next_run = next_run + timedelta(days=interval_days)
                
                wait_seconds = (next_run - now).total_seconds()
                print(f"[Cleanup] 清理间隔: {interval_days}天, 执行时间: {run_hour}:00")
                print(f"[Cleanup] 下次清理时间: {next_run}, 等待 {wait_seconds/3600:.1f} 小时")
                
                await asyncio.sleep(wait_seconds)
                
                if cls._running:
                    await cls.run_cleanup()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Cleanup] 清理循环错误: {e}")
                # 出错后等待 1 小时再重试
                await asyncio.sleep(3600)
    
    @classmethod
    def start_background_task(cls):
        """启动后台清理任务"""
        settings = get_settings()
        
        if not settings.cleanup.enabled:
            print("[Cleanup] 自动清理已禁用")
            return
        
        if cls._task is None or cls._task.done():
            cls._running = True
            cls._task = asyncio.create_task(cls._cleanup_loop())
            print(f"[Cleanup] 后台清理任务已启动 (间隔: {settings.cleanup.interval_days}天, 时间: {settings.cleanup.run_hour}:00)")
    
    @classmethod
    def stop_background_task(cls):
        """停止后台清理任务"""
        cls._running = False
        if cls._task and not cls._task.done():
            cls._task.cancel()
            print("[Cleanup] 后台清理任务已停止")
