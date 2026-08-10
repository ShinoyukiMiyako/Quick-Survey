"""对外时间序列化.

库内所有 DateTime 列都是 naive UTC (写入侧统一 datetime.now(timezone.utc), SQLite
会丢掉 tzinfo 只留 UTC 墙钟)。直接 .isoformat() 出去的串不带时区标记, 而 JS 的
`new Date("2026-08-09T20:34:20")` 按规范把无标记的串当**本地时间**解析 —— 面板与玩家端
于是把 UTC 当北京时间显示, 整体差 8 小时 (跨零点还会退一天)。

所以出站一律用 iso_utc: naive 补 UTC 标记, aware 原样输出, 结果形如
"2026-08-09T20:34:20+00:00", 由前端按浏览器时区渲染。
"""
from datetime import datetime, timezone
from typing import Optional


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """datetime -> 带时区标记的 ISO 串; naive 视为 UTC。None 原样返回。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
