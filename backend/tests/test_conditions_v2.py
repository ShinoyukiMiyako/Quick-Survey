"""条件引擎 v2 (app.services.conditions) 单测。

覆盖三件事: 旧形态 {depends_on, show_when} 的既有语义必须原样保留;
新形态 {action, match, rules} 的 9 个运算符各自的真假分支; 悬空规则的降级行为。
删掉对应的求值分支, 这里的断言必须挂掉。
"""
from types import SimpleNamespace as NS

from app.services.conditions import is_question_visible, normalize_condition


class _Placeholder:
    """不带 .type 的题占位对象: 既有调用方就是这么传的, 引擎必须能按答案形态推断。"""


def _qmap(*pairs) -> dict:
    """构造 {question_id: 题对象}, 入参为 (question_id, type) 二元组。"""
    return {qid: NS(id=qid, type=qtype) for qid, qtype in pairs}


def _placeholder_map(*ids) -> dict:
    return {qid: _Placeholder() for qid in ids}


# === 旧形态: 既有语义逐条保留 ===

def test_legacy_missing_keys_is_always_visible():
    assert is_question_visible(None, {}, {}) is True
    assert is_question_visible({}, {}, {}) is True
    # 只有半边键视为未配置条件
    assert is_question_visible({"show_when": "A"}, {}, {}) is True
    assert is_question_visible({"depends_on": 1}, {}, {}) is True


def test_legacy_single_value_hit_and_miss():
    qm = _qmap((7, "single"))
    assert is_question_visible({"depends_on": 7, "show_when": "yes"}, {7: {"value": "yes"}}, qm) is True
    assert is_question_visible({"depends_on": 7, "show_when": "no"}, {7: {"value": "yes"}}, qm) is False


def test_legacy_multi_candidate_values_hit_any():
    qm = _qmap((7, "single"))
    am = {7: {"value": "B"}}
    assert is_question_visible({"depends_on": 7, "show_when": ["A", "B"]}, am, qm) is True
    assert is_question_visible({"depends_on": 7, "show_when": ["C", "D"]}, am, qm) is False


def test_legacy_multiple_choice_dependency_hits_any():
    qm = _qmap((7, "multiple"))
    am = {7: {"values": ["x", "y", "z"]}}
    assert is_question_visible({"depends_on": 7, "show_when": ["y"]}, am, qm) is True
    assert is_question_visible({"depends_on": 7, "show_when": ["q"]}, am, qm) is False


def test_legacy_text_dependency():
    qm = _qmap((7, "text"))
    assert is_question_visible({"depends_on": 7, "show_when": "hello"}, {7: {"text": "hello"}}, qm) is True
    assert is_question_visible({"depends_on": 7, "show_when": "hello"}, {7: {"text": "bye"}}, qm) is False


def test_legacy_dependency_unanswered_or_empty_is_hidden():
    qm = _qmap((7, "single"))
    assert is_question_visible({"depends_on": 7, "show_when": "A"}, {}, qm) is False
    assert is_question_visible({"depends_on": 7, "show_when": "A"}, {7: {"value": ""}}, qm) is False


def test_legacy_missing_dependency_question_stays_visible():
    qm = _qmap((1, "single"), (2, "single"))
    assert is_question_visible({"depends_on": 99, "show_when": "A"}, {99: {"value": "A"}}, qm) is True


def test_legacy_dependency_id_is_question_id_not_index():
    qm = _qmap((1, "single"), (2, "single"))
    am = {1: {"value": "A"}, 2: {"value": "B"}}
    assert is_question_visible({"depends_on": 1, "show_when": "A"}, am, qm) is True
    assert is_question_visible({"depends_on": 1, "show_when": "B"}, am, qm) is False


def test_legacy_works_without_question_type_metadata():
    qm = _placeholder_map(7)
    assert is_question_visible({"depends_on": 7, "show_when": "y"}, {7: {"values": ["x", "y"]}}, qm) is True
    assert is_question_visible({"depends_on": 7, "show_when": "hi"}, {7: {"text": "hi"}}, qm) is True


# === boolean 归一化: 修复 str(True) == "True" 永不等于前端 "true" 的 bug ===

def test_boolean_dependency_matches_lowercase_true():
    qm = _qmap((3, "boolean"))
    assert is_question_visible({"depends_on": 3, "show_when": "true"}, {3: {"value": True}}, qm) is True
    assert is_question_visible({"depends_on": 3, "show_when": "false"}, {3: {"value": True}}, qm) is False
    assert is_question_visible({"depends_on": 3, "show_when": "false"}, {3: {"value": False}}, qm) is True


def test_boolean_normalization_without_type_metadata():
    # 题型缺失时走形态推断分支, 同样必须归一化成小写字面量
    qm = _placeholder_map(3)
    assert is_question_visible({"depends_on": 3, "show_when": "true"}, {3: {"value": True}}, qm) is True
    assert is_question_visible({"depends_on": 3, "show_when": "false"}, {3: {"value": False}}, qm) is True


def test_boolean_false_counts_as_answered():
    qm = _qmap((3, "boolean"))
    cond = {"rules": [{"question_id": 3, "operator": "answered"}]}
    assert is_question_visible(cond, {3: {"value": False}}, qm) is True


# === normalize_condition ===

def test_normalize_legacy_shape():
    assert normalize_condition({"depends_on": 12, "show_when": "A"}) == {
        "action": "show",
        "match": "any",
        "rules": [{"question_id": 12, "operator": "in", "value": ["A"]}],
    }
    assert normalize_condition({"depends_on": "12", "show_when": ["A", "B"]}) == {
        "action": "show",
        "match": "any",
        "rules": [{"question_id": 12, "operator": "in", "value": ["A", "B"]}],
    }


def test_normalize_new_shape_fills_defaults():
    assert normalize_condition({"rules": [{"question_id": 5, "operator": "eq", "value": "A"}]}) == {
        "action": "show",
        "match": "all",
        "rules": [{"question_id": 5, "operator": "eq", "value": "A"}],
    }
    # 非法 action/match 回落到缺省, 而不是原样带进求值
    got = normalize_condition({"action": "toggle", "match": "some", "rules": [{"question_id": 5}]})
    assert got["action"] == "show" and got["match"] == "all"
    assert got["rules"] == [{"question_id": 5, "operator": "eq", "value": None}]


def test_normalize_rejects_invalid_or_empty():
    assert normalize_condition(None) is None
    assert normalize_condition({}) is None
    assert normalize_condition({"rules": []}) is None
    assert normalize_condition({"rules": [{"operator": "eq", "value": "A"}]}) is None
    assert normalize_condition({"rules": [{"question_id": 1, "operator": "regex"}]}) is None
    assert normalize_condition("not-a-dict") is None


def test_normalize_drops_value_for_valueless_operators():
    got = normalize_condition({"rules": [{"question_id": 1, "operator": "answered", "value": "忽略我"}]})
    assert got["rules"] == [{"question_id": 1, "operator": "answered", "value": None}]


# === 新形态: match / action ===

def _two_rule_condition(match: str, second_value: str, action: str = "show") -> dict:
    return {
        "action": action,
        "match": match,
        "rules": [
            {"question_id": 1, "operator": "eq", "value": "A"},
            {"question_id": 2, "operator": "eq", "value": second_value},
        ],
    }


def test_match_all_requires_every_rule():
    qm = _qmap((1, "single"), (2, "single"))
    am = {1: {"value": "A"}, 2: {"value": "B"}}
    assert is_question_visible(_two_rule_condition("all", "B"), am, qm) is True
    assert is_question_visible(_two_rule_condition("all", "Z"), am, qm) is False


def test_match_any_requires_only_one_rule():
    qm = _qmap((1, "single"), (2, "single"))
    am = {1: {"value": "A"}, 2: {"value": "B"}}
    # 第二条不成立, any 下仍可见
    assert is_question_visible(_two_rule_condition("any", "Z"), am, qm) is True
    # 反过来第一条不成立、第二条成立, 同样可见
    assert is_question_visible(_two_rule_condition("any", "B"), {1: {"value": "X"}, 2: {"value": "B"}}, qm) is True
    # 两条都不成立才隐藏
    assert is_question_visible(_two_rule_condition("any", "Z"), {1: {"value": "X"}, 2: {"value": "Y"}}, qm) is False


def test_action_hide_inverts_match_result():
    qm = _qmap((1, "single"), (2, "single"))
    am = {1: {"value": "A"}, 2: {"value": "B"}}
    # 命中即隐藏
    assert is_question_visible(_two_rule_condition("all", "B", action="hide"), am, qm) is False
    # 未命中则显示
    assert is_question_visible(_two_rule_condition("all", "Z", action="hide"), am, qm) is True


# === 逐个运算符: 各一个真分支一个假分支 ===

def _rule_cond(operator: str, value=None, question_id: int = 1) -> dict:
    return {"rules": [{"question_id": question_id, "operator": operator, "value": value}]}


def test_operator_eq():
    qm = _qmap((1, "single"))
    assert is_question_visible(_rule_cond("eq", "A"), {1: {"value": "A"}}, qm) is True
    assert is_question_visible(_rule_cond("eq", "A"), {1: {"value": "B"}}, qm) is False


def test_operator_neq():
    qm = _qmap((1, "single"))
    assert is_question_visible(_rule_cond("neq", "A"), {1: {"value": "B"}}, qm) is True
    assert is_question_visible(_rule_cond("neq", "A"), {1: {"value": "A"}}, qm) is False


def test_operator_eq_on_multiple_choice_hits_any():
    qm = _qmap((1, "multiple"))
    am = {1: {"values": ["x", "y"]}}
    assert is_question_visible(_rule_cond("eq", "y"), am, qm) is True
    assert is_question_visible(_rule_cond("eq", "z"), am, qm) is False


def test_operator_in():
    qm = _qmap((1, "single"))
    assert is_question_visible(_rule_cond("in", ["A", "B"]), {1: {"value": "B"}}, qm) is True
    assert is_question_visible(_rule_cond("in", ["A", "B"]), {1: {"value": "C"}}, qm) is False


def test_operator_not_in():
    qm = _qmap((1, "single"))
    assert is_question_visible(_rule_cond("not_in", ["A", "B"]), {1: {"value": "C"}}, qm) is True
    assert is_question_visible(_rule_cond("not_in", ["A", "B"]), {1: {"value": "A"}}, qm) is False


def test_operator_in_on_multiple_choice_uses_intersection():
    qm = _qmap((1, "multiple"))
    am = {1: {"values": ["x", "y"]}}
    assert is_question_visible(_rule_cond("in", ["y", "z"]), am, qm) is True
    assert is_question_visible(_rule_cond("in", ["z", "w"]), am, qm) is False


def test_operator_contains_is_case_insensitive():
    qm = _qmap((1, "text"))
    assert is_question_visible(_rule_cond("contains", "hello"), {1: {"text": "Say Hello World"}}, qm) is True
    assert is_question_visible(_rule_cond("contains", "bye"), {1: {"text": "Say Hello World"}}, qm) is False


def test_operator_gt():
    qm = _qmap((1, "number"))
    assert is_question_visible(_rule_cond("gt", 5), {1: {"value": 7}}, qm) is True
    assert is_question_visible(_rule_cond("gt", 5), {1: {"value": 3}}, qm) is False
    # 边界: 严格大于, 相等不算
    assert is_question_visible(_rule_cond("gt", 5), {1: {"value": 5}}, qm) is False


def test_operator_lt():
    qm = _qmap((1, "number"))
    assert is_question_visible(_rule_cond("lt", 5), {1: {"value": 3.5}}, qm) is True
    assert is_question_visible(_rule_cond("lt", 5), {1: {"value": 7}}, qm) is False


def test_operator_gt_on_non_numeric_answer_is_false():
    qm = _qmap((1, "short_text"))
    assert is_question_visible(_rule_cond("gt", 5), {1: {"text": "abc"}}, qm) is False
    assert is_question_visible(_rule_cond("lt", 5), {1: {"text": "abc"}}, qm) is False


def test_operator_answered():
    qm = _qmap((1, "single"))
    assert is_question_visible(_rule_cond("answered"), {1: {"value": "A"}}, qm) is True
    assert is_question_visible(_rule_cond("answered"), {1: {"value": ""}}, qm) is False
    assert is_question_visible(_rule_cond("answered"), {}, qm) is False


def test_operator_not_answered():
    qm = _qmap((1, "single"))
    assert is_question_visible(_rule_cond("not_answered"), {}, qm) is True
    assert is_question_visible(_rule_cond("not_answered"), {1: {"value": "A"}}, qm) is False


def test_number_zero_counts_as_answered():
    # 0 是合法作答, 不能被当成空值
    qm = _qmap((1, "number"))
    assert is_question_visible(_rule_cond("answered"), {1: {"value": 0}}, qm) is True
    assert is_question_visible(_rule_cond("lt", 1), {1: {"value": 0}}, qm) is True


# === 悬空规则 ===

def test_all_rules_dangling_stays_visible():
    # 依赖题都不在本卷 (已删 / 随机卷未抽中): 条件已失效, 不能因此把题藏掉
    qm = _qmap((1, "single"))
    cond = {
        "match": "all",
        "rules": [
            {"question_id": 90, "operator": "eq", "value": "A"},
            {"question_id": 91, "operator": "eq", "value": "B"},
        ],
    }
    assert is_question_visible(cond, {}, qm) is True
    assert is_question_visible({**cond, "match": "any"}, {}, qm) is True
    # action=hide 同理: 悬空不构成隐藏理由
    assert is_question_visible({**cond, "action": "hide"}, {}, qm) is True


def test_partially_dangling_rules_under_match_all_skip_the_dangling_one():
    qm = _qmap((1, "single"))
    cond = {
        "match": "all",
        "rules": [
            {"question_id": 1, "operator": "eq", "value": "A"},
            {"question_id": 90, "operator": "eq", "value": "B"},
        ],
    }
    assert is_question_visible(cond, {1: {"value": "A"}}, qm) is True
    assert is_question_visible(cond, {1: {"value": "X"}}, qm) is False


def test_partially_dangling_rules_under_match_any_count_as_false():
    qm = _qmap((1, "single"))
    cond = {
        "match": "any",
        "rules": [
            {"question_id": 1, "operator": "eq", "value": "A"},
            {"question_id": 90, "operator": "eq", "value": "B"},
        ],
    }
    assert is_question_visible(cond, {1: {"value": "A"}}, qm) is True
    # 唯一能判定的规则不成立, 悬空那条不能兜底成 True
    assert is_question_visible(cond, {1: {"value": "X"}}, qm) is False


# === 与玩家端 src/lib/conditions.ts 的差分对拍所钉住的两条语义 ===
# 这两条曾在两端算出相反结果: 后端判可见、前端判隐藏时, 玩家看不到那道必填题,
# 提交必吃 400 且无从自证。改动任一端前, 先跑 scratchpad 的 parity 对拍。

def test_eq_with_list_value_degrades_to_any_hit():
    """eq 配了数组值时按"命中任一"判, 不是拿字面量整体比较。"""
    qm = _qmap((1, "single"))
    cond = {"match": "any", "rules": [{"question_id": 1, "operator": "eq", "value": ["A", "B"]}]}
    assert is_question_visible(cond, {1: {"value": "A"}}, qm) is True
    assert is_question_visible(cond, {1: {"value": "B"}}, qm) is True
    assert is_question_visible(cond, {1: {"value": "C"}}, qm) is False


def test_neq_with_list_value_degrades_to_any_hit():
    qm = _qmap((1, "single"))
    cond = {"match": "any", "rules": [{"question_id": 1, "operator": "neq", "value": ["A", "B"]}]}
    assert is_question_visible(cond, {1: {"value": "A"}}, qm) is False
    assert is_question_visible(cond, {1: {"value": "C"}}, qm) is True


def test_contains_without_usable_keyword_never_matches():
    """空关键词/多值关键词一律不成立: 空串在两端的子串判定里是恒真, 不拦就成了永远显示。"""
    qm = _qmap((1, "text"))
    for bad in ("", "   ", None, ["A", "B"]):
        cond = {"match": "any", "rules": [{"question_id": 1, "operator": "contains", "value": bad}]}
        assert is_question_visible(cond, {1: {"text": "hello world"}}, qm) is False, bad
    ok = {"match": "any", "rules": [{"question_id": 1, "operator": "contains", "value": "world"}]}
    assert is_question_visible(ok, {1: {"text": "hello world"}}, qm) is True
