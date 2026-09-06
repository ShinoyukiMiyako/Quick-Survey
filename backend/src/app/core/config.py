from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache
import yaml
from pathlib import Path


class ServerSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False


class DatabaseSettings(BaseSettings):
    url: str = "sqlite+aiosqlite:///./data/survey.db"


class AuthSettings(BaseSettings):
    admin_password: str = ""
    # 必须与 mod 配置文件里的 jwt-secret 逐字符一致, 用于验证 mod 签发的 HS256 token
    jwt_secret: str = ""


class ModSettings(BaseSettings):
    """Convenient-access mod 后端对接配置 (玩家凭 token 自助领码时, 问卷后端以服务端身份调 mod)。"""
    # mod API 基址, 形如 https://api.mcwok.cn:22222 (不含末尾斜杠); 留空则领码功能不可用。
    api_base: str = ""
    # mod 的 API Token (对应 mod 配置 api-token), 作 X-API-Key 头发送。
    api_key: str = ""


class InternalSettings(BaseSettings):
    """内部接口鉴权 (供 NapCat 插件调用: 查过审 QQ、轮询审核群通知队列)。"""
    # 共享 token, 与 NapCat 插件配置一致; 走 X-Internal-Token 头常量时间比对。留空则内部接口拒绝所有请求。
    token: str = ""


class UploadSettings(BaseSettings):
    path: str = "./uploads"
    allowed_types: list[str] = ["image/jpeg", "image/png", "image/gif", "image/webp"]
    max_size_mb: int = 10
    # 文件题的扩展名白名单 (小写带点)。按扩展名而不是 MIME 把关: .ysm/.zip 这类附件浏览器
    # 一律报 application/octet-stream, MIME 白名单等于全放行。
    # 严禁放开 .html/.htm/.svg/.xhtml: /uploads 与站点同源静态托管, 放进来就是存储型 XSS。
    allowed_file_extensions: list[str] = [".ysm", ".zip", ".7z", ".rar", ".json", ".txt", ".log", ".pdf"]
    # 文件题单个附件上限, 与图片分开: 模型包动辄几十 MB, 用图片的 10MB 卡会全上传不了
    max_file_size_mb: int = 50

    @property
    def max_size_bytes(self) -> int:
        return self.max_size_mb * 1024 * 1024

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


class CorsSettings(BaseSettings):
    allowed_origins: list[str] = ["*"]


class TurnstileSettings(BaseSettings):
    """Cloudflare Turnstile 配置"""
    enabled: bool = False
    secret_key: str = ""
    # siteverify 端点。本机(阿里云广州)出口到 challenges.cloudflare.com 的 TLS 握手会被
    # 中途阻断(ServerHello 后卡死), 实测失败率 85%; 故指向深圳面板机的 nginx 中转。
    # 线路恢复后把本项改回 https://challenges.cloudflare.com/turnstile/v0/siteverify 即可直连。
    verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    # siteverify 网络层不可达/中转异常时是否放行。Turnstile 只是第一道闸, 后面还压着
    # IP 限流、提交耗时检测与人工审核; 真人被挡的代价高于漏放少量机器人, 故默认降级放行。
    fail_open: bool = True


class RateLimitSettings(BaseSettings):
    """IP 提交限制配置"""
    enabled: bool = True
    max_submissions_per_day: int = 2


class TimeCheckSettings(BaseSettings):
    """提交时间检测配置"""
    enabled: bool = True
    min_submit_time: int = 10  # 最小提交时间（秒）


class CleanupSettings(BaseSettings):
    """清理任务配置"""
    enabled: bool = True  # 是否启用自动清理
    interval_days: int = 1  # 清理间隔（天）
    run_hour: int = 3  # 每天执行时间（小时，0-23）
    orphan_file_hours: int = 24  # 孤立文件超过多少小时后删除


class SecuritySettings(BaseSettings):
    """安全配置"""
    turnstile: TurnstileSettings = Field(default_factory=TurnstileSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    time_check: TimeCheckSettings = Field(default_factory=TimeCheckSettings)


class Settings(BaseSettings):
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    mod: ModSettings = Field(default_factory=ModSettings)
    internal: InternalSettings = Field(default_factory=InternalSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    cleanup: CleanupSettings = Field(default_factory=CleanupSettings)


def load_config() -> Settings:
    """从 config.yml 加载配置"""
    config_path = Path("config.yml")
    
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)
        
        # 解析 security 配置
        security_data = config_data.get("security", {})
        security_settings = SecuritySettings(
            turnstile=TurnstileSettings(**security_data.get("turnstile", {})),
            rate_limit=RateLimitSettings(**security_data.get("rate_limit", {})),
            time_check=TimeCheckSettings(**security_data.get("time_check", {})),
        )
        
        return Settings(
            server=ServerSettings(**config_data.get("server", {})),
            database=DatabaseSettings(**config_data.get("database", {})),
            auth=AuthSettings(**config_data.get("auth", {})),
            mod=ModSettings(**config_data.get("mod", {})),
            internal=InternalSettings(**config_data.get("internal", {})),
            upload=UploadSettings(**config_data.get("upload", {})),
            cors=CorsSettings(**config_data.get("cors", {})),
            security=security_settings,
            cleanup=CleanupSettings(**config_data.get("cleanup", {})),
        )
    
    return Settings()


@lru_cache
def get_settings() -> Settings:
    return load_config()
