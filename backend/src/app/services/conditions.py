"""条件显示引擎 v2 (纯函数, 无 DB 依赖)。

存量库里的 condition 全是旧形态 {depends_on, show_when}, 新版编辑器产出的是
{action, match, rules}; 两者在此统一归一化成一种内部形态再求值, 免得后端各调用点
与前端各维护一套分支解释。
"""
from typing import Any

from app.services.question_types import QUESTION_TYPES, comparable_value


CONDITION_ACTIONS = ("show", "hide")
CONDITION_MATCHES = ("all", "any")
CONDITION_OPERATORS = (
    "eq", "neq", "in", "not_in", "contains", "gt", "lt", "answered", "not_answered",
)

# 这两个运算符只看依赖题"有没有作答", 规则里配的 value 一律忽略
_VALUELESS_OPERATORS = ("answered", "not_answered")


def _as_question_id(raw: Any) -> int | None:
    """依赖题 id 容错: 前端表单回传的可能是数字字符串。bool 是 int 的子类, 必须先排除。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _scalar_text(value: Any) -> str | None:
    """标量归一化为可比较字符串; None 与空白串视为未作答。

    bool 必须先于通用分支处理并转成小写 "true"/"false" —— 条件编辑器里判断题的
    取值字面量就是这两个, 直接 str(True) 得到 "True", 与之永不相等。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    return text or None


def _as_float(value: Any) -> float | None:
    """gt/lt 要求双方都是数值, 转不动就让规则判不成立而不是抛错。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _comparable_by_shape(content: dict | None) -> str | list[str] | None:
    """题型未知时按 content 形态推断可比较值 (多选 values / 标量 value / 文本 text)。"""
    if not isinstance(content, dict) or not content:
        return None
    if "values" in content:
        values = content.get("values")
        if not isinstance(values, (list, tuple)):
            return None
        items = [t for t in (_scalar_text(v) for v in values) if t is not None]
        return items or None
    if "value" in content:
        return _scalar_text(content.get("value"))
    if "text" in content:
        return _scalar_text(content.get("text"))
    return None


def _comparable(question: Any, content: dict | None) -> str | list[str] | None:
    """取依赖题答案的可比较形态。

    question_map 的值在部分调用方 (以及既有单测) 里只是不带 .type 的占位对象,
    题型缺失或未知时退回按 content 形态推断, 免得元数据不全就让整条条件失效。
    """
    qtype = getattr(question, "type", None)
    if isinstance(qtype, str) and qtype in QUESTION_TYPES:
        return comparable_value(qtype, content)
    return _comparable_by_shape(content)


def _to_items(value: Any) -> list[str]:
    """把可比较值摊成字符串列表; 未作答摊成空列表, 让"有没有答"只剩一种判定方式。"""
    raw = value if isinstance(value, (list, tuple, set)) else [value]
    return [t for t in (_scalar_text(v) for v in raw) if t is not None]


def _normalize_rule(raw: Any) -> dict | None:
    """单条规则归一化; 缺 question_id 或运算符不认识都判为无效规则。"""
    if not isinstance(raw, dict):
        return None
    question_id = _as_question_id(raw.get("question_id"))
    if question_id is None:
        return None
    operator = raw.get("operator") or "eq"
    if operator not in CONDITION_OPERATORS:
        return None
    value = None if operator in _VALUELESS_OPERATORS else raw.get("value")
    return {"question_id": question_id, "operator": operator, "value": value}


def normalize_condition(condition: dict | None) -> dict | None:
    """把新旧两种 condition 形态统一为 {"action","match","rules"}; 非法或空返回 None。

    旧形态 {depends_on, show_when} 的语义是"依赖题答案命中候选值之一", 等价于
    单条 in 规则; show_when 是多值时按命中任一算, 故 match 取 any (单规则下与 all 同解,
    保留 any 只为语义直白)。
    """
    if not isinstance(condition, dict) or not condition:
        return None

    rules_raw = condition.get("rules")
    if isinstance(rules_raw, (list, tuple)) and rules_raw:
        rules = [r for r in (_normalize_rule(item) for item in rules_raw) if r is not None]
        if not rules:
            return None
        action = condition.get("action")
        match = condition.get("match")
        return {
            "action": action if action in CONDITION_ACTIONS else "show",
            "match": match if match in CONDITION_MATCHES else "all",
            "rules": rules,
        }

    depends_on = _as_question_id(condition.get("depends_on"))
    show_when = condition.get("show_when")
    if depends_on is None or show_when is None:
        return None
    if isinstance(show_when, (list, tuple, set)):
        expected = [str(v) for v in show_when]
    else:
        expected = [str(show_when)]
    return {
        "action": "show",
        "match": "any",
        "rules": [{"question_id": depends_on, "operator": "in", "value": expected}],
    }


def _evaluate_rule(rule: dict, question: Any, content: dict | None) -> bool:
    """求单条规则。依赖题存在但未作答时, 除 not_answered 外一律不成立。"""
    operator = rule["operator"]
    items = _to_items(_comparable(question, content))

    if operator == "answered":
        return bool(items)
    if operator == "not_answered":
        return not items
    if not items:
        return False

    expected = rule.get("value")

    # eq/neq/in/not_in 统一按集合命中判定: 依赖题是多选时 comparable 结果是列表,
    # 标量相等也必须退化成"命中任一", 否则多选依赖永远匹配不上。
    if operator in ("eq", "neq", "in", "not_in"):
        targets = set(_to_items(expected))
        hit = bool(targets & set(items))
        return hit if operator in ("eq", "in") else not hit

    if operator == "contains":
        if isinstance(expected, (list, tuple, set)):
            return False  # 多值关键词无定义; 不写死就会去比 str(['A','B']) 这种字面量
        needle = _scalar_text(expected)
        if needle is None:
            return False  # 关键词没配就当规则未配置, 不做无意义的全命中
        needle = needle.lower()
        return any(needle in item.lower() for item in items)

    if operator in ("gt", "lt"):
        right = _as_float(expected)
        if right is None:
            return False
        for item in items:
            left = _as_float(item)
            if left is None:
                continue
            if left > right if operator == "gt" else left < right:
                return True
        return False

    return False


def is_question_visible(condition, answer_map: dict, question_map: dict) -> bool:
    """依据条件逻辑判断题目是否应对用户可见 (纯函数, 便于单测)。

    answer_map:   {question_id: content}
    question_map: {question_id: question} (题对象需有 .type; 缺失则按答案形态推断)

    规则里的依赖题不在 question_map (题已删 / 随机卷未抽中) 时该规则无从判定:
    match=all 跳过它, match=any 记它不成立; 若全部规则都悬空则整条条件已失效,
    返回 True 不隐藏 —— 与旧引擎"依赖题不存在就照常显示"的行为一致。
    """
    normalized = normalize_condition(condition)
    if normalized is None:
        return True

    match = normalized["match"]
    rules = normalized["rules"]
    outcomes: list[bool] = []
    dangling = 0

    for rule in rules:
        question = question_map.get(rule["question_id"])
        if question is None:
            dangling += 1
            if match == "any":
                outcomes.append(False)
            continue
        outcomes.append(_evaluate_rule(rule, question, answer_map.get(rule["question_id"])))

    if dangling == len(rules):
        return True

    matched = all(outcomes) if match == "all" else any(outcomes)
    return not matched if normalized["action"] == "hide" else matched
