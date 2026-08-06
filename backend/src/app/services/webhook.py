"""
提交后 webhook 推送。

尽力而为的副作用: 目标站点挂了/超时/返回 500 都只记 warning, 绝不把异常抛回提交流程 ——
玩家的提交已经落库, 不能因为第三方接收端不可用而让他看到提交失败。
"""
import ipaddress
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

EVENT_SUBMISSION_CREATED = "submission.created"

# 非 IP 字面量时按域拦截的清单: 命中本身或其子域都算内网。RFC 6761 规定 .localhost 整棵子树
# 都必须解析到本机, 所以 admin.localhost 与 localhost 同样危险, 不能只做精确匹配。
# 127.0.0.0/8 / 10.0.0.0/8 / 172.16.0.0/12 / 192.168.0.0/16 / 169.254.0.0/16 / ::1 这些网段
# 不写在这里 —— 字符串匹配挡不住等价写法, 一律交给 ipaddress 按解析后的地址判定。
BLOCKED_HOST_SUFFIXES = ("localhost",)

# 接收端再慢也不该拖住提交响应
REQUEST_TIMEOUT = 5.0


def _is_safe_hostname(host: str) -> bool:
    """ipaddress 解析不了的 host 是否可以放行。

    这里是原先 fail-open 的根因: ipaddress 与真正发起连接的解析器口径不一致。ipaddress 只认
    完整四段点分十进制, 而 Linux glibc 的 inet_aton 还认 127.1 / 127.0.1 / 2130706433 /
    0177.0.0.1 / 0x7f.0.0.1 / 010.1.2.3, 并把它们全部解析成 127.0.0.1。这些写法在 ipaddress
    眼里只是"解析失败的域名", 一旦按域名放行, 请求就实打实地打到了本机端口上。

    判据取最右标签: inet_aton 的等价写法要求每一段都是数字 (十进制/八进制/十六进制), 而 RFC 1123
    禁止全数字顶级域, 真实域名的最右标签不可能是纯数字或 0x 开头。据此可以覆盖全部等价写法而不误伤域名。
    """
    host = host.rstrip(".")  # 根域写法 hooks.example.com. 与不带尾点等价
    if not host:
        return False

    for blocked in BLOCKED_HOST_SUFFIXES:
        if host == blocked or host.endswith("." + blocked):
            return False

    last_label = host.rsplit(".", 1)[-1]
    if last_label.isdigit() or last_label.startswith("0x"):
        return False

    return True


def is_safe_webhook_url(url: str) -> bool:
    """判断 webhook 目标是否可以发出请求 (防 SSRF)。

    webhook_url 由管理员填写, 一旦面板账号被打穿, 这个字段就是打内网的跳板: 后端能访问的
    元数据服务/内网管理口都会被当成"回调目标"。故只放行 http/https 且指向公网地址的 URL。
    这里不做 DNS 解析: 解析会阻塞事件循环, 且解析结果与实际连接之间存在 TOCTOU 竞态,
    对域名只拦本机别名与 IP 等价写法, 剩余风险由部署侧的出网策略兜底。
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False  # 畸形 IPv6 字面量等无法解析的 URL

    if parsed.scheme not in ("http", "https"):
        return False
    if not host:
        return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return _is_safe_hostname(host)

    # 用白名单 is_global 而不是逐条列黑名单: 逐条列会漏掉不属于任何一条的网段, 例如
    # 100.64.0.0/10 (运营商级 NAT, 云厂商元数据 100.100.100.200 就在里面) 的 is_private 是 False,
    # 六项黑名单全不命中就被放行了。is_global 一次覆盖 loopback/private/link-local/reserved/
    # unspecified 与 100.64/10。唯一的例外是组播: CPython 没把 224.0.0.0/4 与 ff00::/8 算进
    # is_global 的排除项 (224.0.0.1 的 is_global 为 True), 必须单独再挡一次。
    return address.is_global and not address.is_multicast


def _build_payload(survey, submission, answers: list) -> dict:
    """组装推送体。

    题目标题/类型从 survey.questions 反查而非 answer.question: 后者是未预加载的关系,
    异步会话里访问会抛 MissingGreenlet。
    """
    question_map = {q.id: q for q in (survey.questions or [])}

    answer_rows = []
    for answer in answers:
        question = question_map.get(answer.question_id)
        answer_rows.append({
            "question_id": answer.question_id,
            "title": question.title if question is not None else None,
            "type": question.type if question is not None else None,
            "content": answer.content,
        })

    return {
        "event": EVENT_SUBMISSION_CREATED,
        "survey": {
            "id": survey.id,
            "code": survey.code,
            "title": survey.title,
        },
        "submission": {
            "id": submission.id,
            "player_name": submission.player_name,
            "qq": submission.qq,
            "status": submission.status,
            "created_at": submission.created_at.isoformat() if submission.created_at else None,
        },
        "answers": answer_rows,
    }


async def dispatch_submission(survey, submission, answers: list) -> None:
    """把一条新提交推给问卷配置的 webhook。失败只记日志, 永不向上抛。"""
    url = (survey.webhook_url or "").strip()

    if not is_safe_webhook_url(url):
        logger.warning("[Webhook] 目标地址缺失或不安全, 跳过推送: survey=%s url=%s", survey.id, url)
        return

    try:
        payload = _build_payload(survey, submission, answers)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(url, json=payload)

        if response.status_code // 100 != 2:
            logger.warning(
                "[Webhook] 目标返回非 2xx: survey=%s status=%s 响应前200字符: %s",
                survey.id, response.status_code, response.text[:200],
            )
    except Exception:
        # 这里的兜底是契约要求的边界 (提交主流程不能被第三方接收端带崩), 不是生吞业务异常
        logger.warning("[Webhook] 推送失败: survey=%s url=%s", survey.id, url, exc_info=True)
