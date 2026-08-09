"""get_real_ip 的信任边界测试。

所有按 IP 的闸门 (每日提交/上传/领码限流、查询与解锁的 per-IP 限流、每卷的每 IP 提交上限)
都建立在这个函数上。它一旦采信来路不明的转发头, 上述闸门全部退化成"请求方自己说了算"。
删掉信任边界判断, 本文件的断言必须挂掉。
"""
from app.core.security import get_real_ip, _TRUSTED_PROXIES


class _Client:
    def __init__(self, host):
        self.host = host


class _Request:
    """只实现 get_real_ip 用到的两个属性 (client / headers)。"""

    def __init__(self, peer, headers=None):
        self.client = _Client(peer) if peer is not None else None
        # Starlette 的 Headers 是大小写不敏感的, 这里用小写键 + 取值时归一化模拟
        self.headers = _Headers(headers or {})


class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def test_direct_connection_ignores_all_forwarding_headers():
    """直连时转发头一律不可信: 只认 TCP 源地址。"""
    request = _Request(
        "203.0.113.7",
        {
            "CF-Connecting-IP": "1.2.3.4",
            "X-Forwarded-For": "1.2.3.4",
            "X-Real-IP": "1.2.3.4",
        },
    )
    assert get_real_ip(request) == "203.0.113.7"


def test_cf_connecting_ip_is_never_trusted():
    """CF-Connecting-IP 已不再参与判定: 线上 nginx 从不设置它, 只可能由客户端自填。

    即便请求来自受信反代, 它也不该盖过 nginx 写的 X-Real-IP。
    """
    request = _Request("127.0.0.1", {"CF-Connecting-IP": "1.2.3.4", "X-Real-IP": "203.0.113.7"})
    assert get_real_ip(request) == "203.0.113.7"

    # 只有伪造的 CF 头、没有 nginx 的 X-Real-IP 时, 结果必须退回反代地址而不是伪造值
    only_cf = _Request("127.0.0.1", {"CF-Connecting-IP": "1.2.3.4"})
    assert get_real_ip(only_cf) == "127.0.0.1"


def test_trusted_proxy_uses_x_real_ip():
    """经本机 nginx 转发时以 X-Real-IP 为准 (nginx 用 $remote_addr 硬覆盖该头)。"""
    for peer in _TRUSTED_PROXIES:
        request = _Request(peer, {"X-Real-IP": " 203.0.113.7 "})
        assert get_real_ip(request) == "203.0.113.7"


def test_x_forwarded_for_takes_rightmost_segment():
    """XFF 取最右段。

    $proxy_add_x_forwarded_for 的形态是 "客户端自填段, ..., nginx 追加的 $remote_addr",
    只有最右一段出自反代之手; 取最左恰好是取唯一可被伪造的那段。
    """
    request = _Request("127.0.0.1", {"X-Forwarded-For": "1.2.3.4, 5.6.7.8, 203.0.113.7"})
    assert get_real_ip(request) == "203.0.113.7"

    single = _Request("127.0.0.1", {"X-Forwarded-For": "203.0.113.7"})
    assert get_real_ip(single) == "203.0.113.7"


def test_x_real_ip_wins_over_x_forwarded_for():
    """两个头都在时以 X-Real-IP 为准: 它是 nginx 单值硬覆盖的, 不含客户端自填段。"""
    request = _Request("127.0.0.1", {"X-Real-IP": "203.0.113.7", "X-Forwarded-For": "1.2.3.4"})
    assert get_real_ip(request) == "203.0.113.7"


def test_falls_back_to_peer_without_headers():
    """受信反代但没带任何转发头时退回对端地址, 不返回 None (返回 None 会让限流整体失效)。"""
    assert get_real_ip(_Request("127.0.0.1")) == "127.0.0.1"
    # XFF 存在但内容为空白, 同样不能返回空串
    assert get_real_ip(_Request("127.0.0.1", {"X-Forwarded-For": "   "})) == "127.0.0.1"


def test_no_client_returns_none():
    """拿不到对端信息时返回 None, 由限流侧按"无 IP"跳过, 而不是抛异常打断请求。"""
    assert get_real_ip(_Request(None)) is None


def test_spoofed_headers_cannot_split_rate_limit_buckets():
    """闸门语义断言: 同一个直连来源换任意转发头, 必须始终归到同一个限流桶。

    这是本次修复的目的 —— 此前每换一个 CF-Connecting-IP 就是一个新桶, 限流形同虚设。
    """
    seen = {
        get_real_ip(_Request("198.51.100.9", {"CF-Connecting-IP": f"1.2.3.{n}"}))
        for n in range(10)
    }
    assert seen == {"198.51.100.9"}
