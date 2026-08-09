"""提交后 webhook 推送的单元测试: SSRF 拦截 + 尽力而为语义 (失败绝不冒泡)。"""
import logging
from datetime import datetime
from types import SimpleNamespace as NS

import httpx
import pytest

from app.services import webhook


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = "接收端响应体"


def _client_factory(calls: list, status_code: int = 200, error=None):
    """造一个 httpx.AsyncClient 替身: 把构造参数与 POST 调用记进 calls, 可指定返回码或抛错。"""

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, url, json=None):
            calls.append({"post": url, "json": json})
            if error is not None:
                raise error
            return _FakeResponse(status_code)

    return _FakeAsyncClient


def _survey(webhook_url: str) -> NS:
    return NS(
        id=7,
        code="abc12345",
        title="活动报名",
        webhook_url=webhook_url,
        questions=[
            NS(id=3, title="留言", type="text"),
            NS(id=4, title="口味", type="single"),
        ],
    )


def _submission() -> NS:
    return NS(
        id=9,
        player_name="Alice",
        qq="10001",
        status="approved",
        created_at=datetime(2026, 8, 6, 12, 30, 0),
    )


def _answers() -> list:
    return [
        NS(question_id=3, content={"text": "来了"}),
        NS(question_id=4, content={"value": "A"}),
        # 题目已被删除的历史答案: 反查不到题时 title/type 落 None, 不能因此炸掉推送
        NS(question_id=99, content={"text": "孤儿答案"}),
    ]


def test_is_safe_webhook_url_blocks_internal_targets():
    assert webhook.is_safe_webhook_url("http://127.0.0.1") is False
    assert webhook.is_safe_webhook_url("http://10.1.2.3") is False
    assert webhook.is_safe_webhook_url("http://localhost/x") is False
    assert webhook.is_safe_webhook_url("ftp://x") is False
    assert webhook.is_safe_webhook_url("https://hooks.example.com") is True


def test_is_safe_webhook_url_covers_other_private_forms():
    # 十进制/十六进制等价写法与 IPv6 环回, 字符串前缀匹配挡不住, 必须靠 ipaddress 解析
    assert webhook.is_safe_webhook_url("http://172.20.0.5:8080/hook") is False
    assert webhook.is_safe_webhook_url("http://192.168.1.1") is False
    assert webhook.is_safe_webhook_url("http://169.254.169.254/latest/meta-data") is False
    assert webhook.is_safe_webhook_url("http://[::1]:9000/hook") is False
    assert webhook.is_safe_webhook_url("http://0.0.0.0") is False
    assert webhook.is_safe_webhook_url("") is False
    assert webhook.is_safe_webhook_url("https:///onlypath") is False
    assert webhook.is_safe_webhook_url("http://8.8.8.8/hook") is True
    assert webhook.is_safe_webhook_url("https://hooks.example.com:8443/a/b?c=1") is True


def test_is_safe_webhook_url_blocks_inet_aton_equivalents():
    # ipaddress 解析不了但 glibc inet_aton 会当成 127.0.0.1 的等价写法。
    # 按"域名"放行等于把请求打回本机端口, 必须一条不落地拦下。
    assert webhook.is_safe_webhook_url("http://127.1/hook") is False
    assert webhook.is_safe_webhook_url("http://127.0.1/hook") is False
    assert webhook.is_safe_webhook_url("http://2130706433/hook") is False
    assert webhook.is_safe_webhook_url("http://0177.0.0.1/hook") is False
    assert webhook.is_safe_webhook_url("http://0x7f.0.0.1/hook") is False
    assert webhook.is_safe_webhook_url("http://010.1.2.3/hook") is False
    # 纯十六进制单标签与带端口的形式同样要挡住
    assert webhook.is_safe_webhook_url("http://0x7f000001:8080/hook") is False
    assert webhook.is_safe_webhook_url("http://127.1:9000/hook") is False


def test_is_safe_webhook_url_blocks_localhost_subtree_and_cgnat():
    # RFC 6761: .localhost 整棵子树都指向本机, 精确匹配 localhost 会漏掉子域
    assert webhook.is_safe_webhook_url("http://admin.localhost/hook") is False
    assert webhook.is_safe_webhook_url("http://a.b.localhost:8080/hook") is False
    assert webhook.is_safe_webhook_url("http://localhost./hook") is False
    # 100.64.0.0/10 的 is_private 为 False, 逐条黑名单判不出来, 而云厂商元数据就住在这儿
    assert webhook.is_safe_webhook_url("http://100.100.100.200/latest/meta-data") is False
    assert webhook.is_safe_webhook_url("http://100.64.0.1/hook") is False
    # 组播不在 is_global 的排除项里, 换白名单后仍必须被单独挡住
    assert webhook.is_safe_webhook_url("http://224.0.0.1/hook") is False
    assert webhook.is_safe_webhook_url("http://[ff02::1]/hook") is False
    # IPv4 映射的 IPv6 写法绕不过去
    assert webhook.is_safe_webhook_url("http://[::ffff:169.254.169.254]/hook") is False


def test_is_safe_webhook_url_keeps_public_targets_allowed():
    # 收紧规则不得误伤正常回调地址
    assert webhook.is_safe_webhook_url("https://hooks.example.com") is True
    assert webhook.is_safe_webhook_url("https://hooks.example.com.") is True
    assert webhook.is_safe_webhook_url("http://8.8.8.8") is True
    assert webhook.is_safe_webhook_url("https://qq-bot.example.com:8443/webhook/mc?token=a1b2") is True
    assert webhook.is_safe_webhook_url("https://sub.domain.example.co.uk/a/b") is True
    assert webhook.is_safe_webhook_url("https://[2001:4860:4860::8888]/hook") is True
    assert webhook.is_safe_webhook_url("http://xn--fiqs8s.example/hook") is True


async def test_dispatch_skips_unsafe_url_without_any_request(monkeypatch, caplog):
    calls: list = []
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _client_factory(calls))
    caplog.set_level(logging.WARNING, logger="app.services.webhook")

    await webhook.dispatch_submission(_survey("http://127.0.0.1/hook"), _submission(), _answers())

    assert calls == []  # 一次 HTTP 都不该发出
    assert "不安全" in caplog.text


async def test_dispatch_skips_when_url_missing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _client_factory(calls))

    await webhook.dispatch_submission(_survey(None), _submission(), _answers())
    await webhook.dispatch_submission(_survey("   "), _submission(), _answers())

    assert calls == []


async def test_dispatch_posts_contract_payload(monkeypatch):
    calls: list = []
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _client_factory(calls))

    await webhook.dispatch_submission(
        _survey("https://hooks.example.com/inbox"), _submission(), _answers()
    )

    assert calls[0]["init"]["timeout"] == 5.0
    assert calls[1]["post"] == "https://hooks.example.com/inbox"

    payload = calls[1]["json"]
    assert payload["event"] == "submission.created"
    assert payload["survey"] == {"id": 7, "code": "abc12345", "title": "活动报名"}
    assert payload["submission"] == {
        "id": 9,
        "player_name": "Alice",
        "qq": "10001",
        "status": "approved",
        "created_at": "2026-08-06T12:30:00",
    }
    assert payload["answers"] == [
        {"question_id": 3, "title": "留言", "type": "text", "content": {"text": "来了"}},
        {"question_id": 4, "title": "口味", "type": "single", "content": {"value": "A"}},
        {"question_id": 99, "title": None, "type": None, "content": {"text": "孤儿答案"}},
    ]


async def test_dispatch_swallows_transport_error(monkeypatch, caplog):
    calls: list = []
    monkeypatch.setattr(
        webhook.httpx, "AsyncClient",
        _client_factory(calls, error=httpx.ConnectError("接收端不可达")),
    )
    caplog.set_level(logging.WARNING, logger="app.services.webhook")

    # 抛异常也不得向上冒泡, 否则玩家已落库的提交会被判失败
    assert await webhook.dispatch_submission(
        _survey("https://hooks.example.com/inbox"), _submission(), _answers()
    ) is None

    assert calls[1]["post"] == "https://hooks.example.com/inbox"  # 确实尝试过发送
    assert "推送失败" in caplog.text


async def test_dispatch_logs_non_2xx_without_raising(monkeypatch, caplog):
    calls: list = []
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _client_factory(calls, status_code=500))
    caplog.set_level(logging.WARNING, logger="app.services.webhook")

    assert await webhook.dispatch_submission(
        _survey("https://hooks.example.com/inbox"), _submission(), _answers()
    ) is None

    assert "非 2xx" in caplog.text
    assert "status=500" in caplog.text


@pytest.mark.parametrize("status_code", [200, 201, 204, 299])
async def test_dispatch_treats_all_2xx_as_success(monkeypatch, caplog, status_code):
    calls: list = []
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _client_factory(calls, status_code=status_code))
    caplog.set_level(logging.WARNING, logger="app.services.webhook")

    await webhook.dispatch_submission(
        _survey("https://hooks.example.com/inbox"), _submission(), _answers()
    )

    # 只看 webhook 自己的日志: caplog.text 是全局的, 别处 (如连接被 GC 回收时 SQLAlchemy
    # 打的 ERROR) 飘来一条就会让这里假失败, 且只在全量跑时复现
    assert [r for r in caplog.records if r.name == "app.services.webhook"] == []
