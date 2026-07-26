"""
安全模块 - 处理 Turnstile 验证、提交时间检测
IP 限流已移至 rate_limit.py 模块
"""
import logging
import time
from typing import Optional
import httpx
from fastapi import HTTPException

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _degrade_or_reject(reason: str) -> bool:
    """
    siteverify 不可用时的降级决策。

    fail_open 打开时放行并留 WARNING 痕迹 (可据此统计降级次数), 否则拒绝提交。
    """
    settings = get_settings()

    if settings.security.turnstile.fail_open or settings.server.debug:
        logger.warning(f"[Turnstile] 降级放行, 本次提交未经人机校验 (原因: {reason})")
        return True

    logger.error(f"[Turnstile] 拒绝提交 (原因: {reason})")
    raise HTTPException(status_code=500, detail="安全验证服务暂时不可用")


async def verify_turnstile(token: str, ip: Optional[str] = None) -> bool:
    """
    验证 Cloudflare Turnstile token
    
    Args:
        token: 前端传来的 Turnstile token
        ip: 用户 IP 地址（可选，用于增强验证）
    
    Returns:
        验证是否成功
    """
    settings = get_settings()
    
    if not settings.security.turnstile.enabled:
        return True
    
    if not token:
        raise HTTPException(status_code=400, detail="缺少安全验证 token")
    
    secret_key = settings.security.turnstile.secret_key
    if not secret_key:
        # 未配置密钥，跳过验证（开发环境）
        return True
    
    verify_url = settings.security.turnstile.verify_url

    try:
        logger.info(f"[Turnstile] 开始验证, IP: {ip}, token长度: {len(token) if token else 0}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                verify_url,
                data={
                    "secret": secret_key,
                    "response": token,
                    **({"remoteip": ip} if ip else {}),
                },
                timeout=10.0,
            )

            # 以"能否解析出 JSON"而非状态码区分业务响应与中转故障:
            # Cloudflare 对格式错误的 secret 会返回 400 + 合法 JSON (属业务响应, 要照常报错),
            # 而中转 nginx 故障 (403/502) 返回的是 HTML 错误页, 只能走降级。
            try:
                result = response.json()
            except ValueError:
                return _degrade_or_reject(
                    f"siteverify 响应非 JSON (HTTP {response.status_code}), 端点 {verify_url}, "
                    f"响应前200字符: {response.text[:200]}"
                )

            logger.info(f"[Turnstile] 验证结果: {result}")
            
            if not result.get("success"):
                error_codes = result.get("error-codes", [])
                logger.warning(f"[Turnstile] 验证失败: {error_codes}, token前20字符: {token[:20] if token else 'None'}...")
                
                # 提供更友好的错误信息
                error_messages = {
                    "missing-input-secret": "服务器配置错误: 缺少密钥",
                    "invalid-input-secret": "服务器配置错误: 密钥无效",
                    "missing-input-response": "缺少验证token",
                    "invalid-input-response": "验证token无效或已过期",
                    "bad-request": "请求格式错误",
                    "timeout-or-duplicate": "验证已过期或重复使用，请刷新页面重试",
                    "internal-error": "Cloudflare服务内部错误",
                }
                
                user_message = "安全验证失败"
                if error_codes:
                    for code in error_codes:
                        if code in error_messages:
                            user_message = error_messages[code]
                            break
                    else:
                        user_message = f"安全验证失败: {', '.join(error_codes)}"
                
                raise HTTPException(status_code=400, detail=user_message)
            
            logger.info(f"[Turnstile] 验证成功")
            return True
            
    except httpx.RequestError as e:
        # ConnectTimeout 一类异常的 str() 为空 (线上日志曾出现"网络错误:"后无内容),
        # 补类型名才能区分是连不上、握手超时还是读超时。
        logger.error(f"[Turnstile] 网络错误: {type(e).__name__}: {e}")
        return _degrade_or_reject(f"网络错误 {type(e).__name__}: {e}")


def check_submit_time(start_time: Optional[float]) -> float:
    """
    检查提交时间是否合理，并返回填写耗时
    
    Args:
        start_time: 用户开始填写问卷的时间戳（秒）
    
    Returns:
        填写耗时（秒），如果没有开始时间则返回 0
    
    Raises:
        HTTPException: 提交时间过短时抛出
    """
    settings = get_settings()
    
    if start_time is None:
        # 没有开始时间，跳过检测
        return 0.0
    
    elapsed = time.time() - start_time
    
    if settings.security.time_check.enabled:
        min_time = settings.security.time_check.min_submit_time
        if elapsed < min_time:
            raise HTTPException(
                status_code=400, 
                detail=f"提交时间过短（{elapsed:.1f}秒），请认真填写问卷"
            )
    
    return elapsed


def get_real_ip(request) -> Optional[str]:
    """
    获取用户真实 IP 地址
    
    支持常见的代理头部：
    - X-Forwarded-For
    - X-Real-IP
    - CF-Connecting-IP (Cloudflare)
    """
    # Cloudflare
    if cf_ip := request.headers.get("CF-Connecting-IP"):
        return cf_ip
    
    # X-Forwarded-For (可能包含多个 IP，取第一个)
    if forwarded := request.headers.get("X-Forwarded-For"):
        return forwarded.split(",")[0].strip()
    
    # X-Real-IP
    if real_ip := request.headers.get("X-Real-IP"):
        return real_ip
    
    # 直连
    if request.client:
        return request.client.host
    
    return None


def get_security_config() -> dict:
    """
    获取前端需要的安全配置
    """
    settings = get_settings()
    
    return {
        "turnstile_enabled": settings.security.turnstile.enabled,
        "time_check_enabled": settings.security.time_check.enabled,
        "min_submit_time": settings.security.time_check.min_submit_time if settings.security.time_check.enabled else 0,
    }
