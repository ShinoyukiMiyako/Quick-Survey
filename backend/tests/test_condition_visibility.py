"""条件题可见性 (is_question_visible) 单测。

覆盖 depends_on 从"按 id 排序的下标"改为"依赖题 question_id"后的语义:
删掉核心比较逻辑, 这些断言必须挂掉。
"""
from app.services.survey import is_question_visible


class _Q:
    """条件求值只需要 question_map 的键存在; 值放个占位对象即可。"""


def _qmap(*ids):
    return {i: _Q() for i in ids}


def test_no_condition_always_visible():
    assert is_question_visible(None, {}, {}) is True
    assert is_question_visible({}, {}, {}) is True
    # 缺 depends_on 或 show_when 视为无条件
    assert is_question_visible({"show_when": "A"}, {}, {}) is True
    assert is_question_visible({"depends_on": 1}, {}, {}) is True


def test_depends_on_is_question_id_not_positional_index():
    # 题 id = {1, 2}; depends_on=1 必须命中 id==1 的题,
    # 而非旧语义"按 id 升序后下标 1"(那会指向 id==2 的题)。
    qm = _qmap(1, 2)
    am = {1: {"value": "A"}, 2: {"value": "B"}}
    assert is_question_visible({"depends_on": 1, "show_when": "A"}, am, qm) is True
    # 若仍按下标解释, depends_on=1 会指向 id2(值 B), 命中 'A' 会失败
    assert is_question_visible({"depends_on": 1, "show_when": "B"}, am, qm) is False


def test_single_value_match_and_mismatch():
    qm = _qmap(7)
    assert is_question_visible({"depends_on": 7, "show_when": "yes"}, {7: {"value": "yes"}}, qm) is True
    assert is_question_visible({"depends_on": 7, "show_when": "yes"}, {7: {"value": "no"}}, qm) is False


def test_multiple_values_hit_any():
    qm = _qmap(7)
    am = {7: {"values": ["x", "y", "z"]}}
    assert is_question_visible({"depends_on": 7, "show_when": ["y"]}, am, qm) is True
    assert is_question_visible({"depends_on": 7, "show_when": ["q"]}, am, qm) is False


def test_text_answer_match():
    qm = _qmap(7)
    assert is_question_visible({"depends_on": 7, "show_when": "hello"}, {7: {"text": "hello"}}, qm) is True
    assert is_question_visible({"depends_on": 7, "show_when": "hello"}, {7: {"text": "bye"}}, qm) is False


def test_dependency_unanswered_or_empty_is_hidden():
    qm = _qmap(7)
    assert is_question_visible({"depends_on": 7, "show_when": "A"}, {}, qm) is False
    assert is_question_visible({"depends_on": 7, "show_when": "A"}, {7: {"value": ""}}, qm) is False


def test_missing_dependency_question_stays_visible():
    # depends_on 指向本卷不存在的题 (已删 / 随机未抽中) -> 不因条件隐藏
    qm = _qmap(1, 2)
    assert is_question_visible({"depends_on": 99, "show_when": "A"}, {99: {"value": "A"}}, qm) is True
