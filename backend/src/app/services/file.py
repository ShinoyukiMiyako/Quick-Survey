import uuid
import aiofiles
from pathlib import Path
from typing import Optional
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import UploadFile, HTTPException, status

from app.core.config import get_settings
from app.models import UploadedFile


class FileService:
    """文件上传服务"""
    
    @staticmethod
    def get_upload_dir() -> Path:
        """获取上传目录"""
        settings = get_settings()
        upload_path = Path(settings.upload.path)
        upload_path.mkdir(parents=True, exist_ok=True)
        return upload_path
    
    @staticmethod
    def generate_filename(original_name: str) -> str:
        """生成唯一文件名"""
        ext = Path(original_name).suffix.lower()
        unique_id = uuid.uuid4().hex[:16]
        timestamp = datetime.now().strftime("%Y%m%d")
        return f"{timestamp}_{unique_id}{ext}"
    
    @classmethod
    async def save_file(
        cls,
        db: AsyncSession,
        file: UploadFile,
        submission_id: Optional[int] = None,
    ) -> UploadedFile:
        """保存上传的文件"""
        settings = get_settings()
        
        # 验证文件类型
        if file.content_type not in settings.upload.allowed_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"不支持的文件类型: {file.content_type}"
            )
        
        # 读取文件内容
        content = await file.read()
        file_size = len(content)
        
        # 验证文件大小
        if file_size > settings.upload.max_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"文件大小超过限制: {settings.upload.max_size_mb}MB"
            )
        
        return await cls._persist(db, file, content, "upload.jpg", submission_id)

    @classmethod
    async def save_attachment(
        cls,
        db: AsyncSession,
        file: UploadFile,
        submission_id: Optional[int] = None,
    ) -> UploadedFile:
        """保存文件题附件。

        与图片走两套把关: 附件按扩展名白名单判定 (浏览器给 .ysm/.zip 一律报
        application/octet-stream, MIME 白名单形同虚设), 体积走更宽的 max_file_size_mb。
        """
        settings = get_settings()

        ext = Path(file.filename or "").suffix.lower()
        allowed = [e.lower() for e in settings.upload.allowed_file_extensions]
        if not ext or ext not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"不支持的文件格式, 仅接受 {' / '.join(allowed)}",
            )

        content = await file.read()
        file_size = len(content)

        if file_size > settings.upload.max_file_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"文件大小超过限制: {settings.upload.max_file_size_mb}MB",
            )
        if file_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="上传的文件是空的",
            )

        return await cls._persist(db, file, content, "upload.bin", submission_id)

    @classmethod
    async def _persist(
        cls,
        db: AsyncSession,
        file: UploadFile,
        content: bytes,
        fallback_name: str,
        submission_id: Optional[int],
    ) -> UploadedFile:
        """落盘 + 建记录。图片与附件的差异只在前面的准入校验, 存储这一段必须共用。"""
        stored_name = cls.generate_filename(file.filename or fallback_name)
        upload_dir = cls.get_upload_dir()
        file_path = upload_dir / stored_name

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)

        uploaded_file = UploadedFile(
            filename=file.filename or fallback_name,
            stored_name=stored_name,
            file_path=str(file_path),
            file_size=len(content),
            mime_type=file.content_type or "application/octet-stream",
            submission_id=submission_id,
        )

        db.add(uploaded_file)
        await db.commit()
        await db.refresh(uploaded_file)

        return uploaded_file


    @staticmethod
    def get_file_url(stored_name: str) -> str:
        """获取文件访问 URL"""
        return f"/uploads/{stored_name}"
