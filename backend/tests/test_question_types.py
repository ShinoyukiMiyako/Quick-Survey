"""题型注册表 (question_types) 单测: 元信息、有效作答判定、内容校验、比较值与 CSV 单元格。

覆盖三个历史坑: number 的 0 / boolean 的 False 必须算已答、boolean 比较值必须是小写
"true"、选项题导出必须映射回 label。删掉对应分支, 这些断言必须挂掉。
"""
import re

import pytest

from app.services.question_types import (
    QUESTION_TYPES,
    QUESTION_TYPE_NAMES,
    QUESTION_TYPE_PATTERN,
    answer_scalar,
    answer_to_cell,
    comparable_value,
    is_answered,
    validate_answer,
)

_OPTIONS = [{"value": "A", "label": "选项A"}, {"value": "B", "label": "选项B"}]


# ==================== 注册表元信息 ====================

def test_registry_covers_all_names():
    # 顺序也锁死: 前端 QuestionType 联合类型与 QUESTION_TYPE_PATTERN 都按这个次序对齐
    assert QUESTION_TYPE_NAMES == (
        "single", "select", "multiple", "boolean", "text",
        "short_text", "number", "date", "rating", "image",
    )
    assert set(QUESTION_TYPES) == set(QUESTION_TYPE_NAMES)
    assert QUESTION_TYPES["multiple"].content_key == "values"
    assert QUESTION_TYPES["image"].content_key == "images"
    assert QUESTION_TYPES["short_text"].content_key == "text"
    assert QUESTION_TYPES["rating"].label == "评分题"


def test_registry_flags():
    bindable = {name for name, spec in QUESTION_TYPES.items() if spec.role_bindable}
    assert bindable == {"single", "select", "text", "short_text"}

    with_options = {name for name, spec in QUESTION_TYPES.items() if spec.needs_options}
    assert with_options == {"single", "select", "multiple"}

    assert QUESTION_TYPES["image"].condition_source is False
    assert all(spec.condition_source for name, spec in QUESTION_TYPES.items() if name != "image")


def test_pattern_matches_exactly_registered_names():
    for name in QUESTION_TYPE_NAMES:
        assert re.match(QUESTION_TYPE_PATTERN, name) is not None
    assert re.match(QUESTION_TYPE_PATTERN, "unknown") is None
    # 必须两端锚定, 否则 pydantic 的 pattern 会放行 "singlex" 这种前缀命中
    assert re.match(QUESTION_TYPE_PATTERN, "singlex") is None


# ==================== validate_answer: 每种题型 合法 + 越界 ====================

def test_validate_single_and_select_against_options():
    validate_answer("single", {"value": "A"}, None, _OPTIONS)
    validate_answer("select", {"value": "B"}, None, _OPTIONS)
    with pytest.raises(ValueError, match="可选范围"):
        validate_answer("single", {"value": "Z"}, None, _OPTIONS)
    with pytest.raises(ValueError, match="可选范围"):
        validate_answer("select", {"value": "Z"}, None, _OPTIONS)
    # 题目没配选项时无从校验, 不能一律拒绝
    validate_answer("single", {"value": "Z"}, None, None)


def test_validate_multiple():
    validate_answer("multiple", {"values": ["A", "B"]}, None, _OPTIONS)
    with pytest.raises(ValueError, match="可选范围"):
        validate_answer("multiple", {"values": ["A", "Z"]}, None, _OPTIONS)
    with pytest.raises(ValueError, match="格式不正确"):
        validate_answer("multiple", {"values": "A"}, None, _OPTIONS)


def test_validate_boolean_requires_real_bool():
    validate_answer("boolean", {"value": True}, None, None)
    validate_answer("boolean", {"value": False}, None, None)
    with pytest.raises(ValueError, match="是或否"):
        validate_answer("boolean", {"value": "maybe"}, None, None)


def test_validate_text_length_uses_stripped_length():
    # 首尾空白不计入字数: "  你好  " 去空白后正好 2 字, 命中闭区间上下限
    validate_answer("text", {"text": "  你好  "}, {"min_length": 2, "max_length": 2}, None)
    with pytest.raises(ValueError, match="最多 2 个字"):
        validate_answer("text", {"text": "你好呀"}, {"max_length": 2}, None)
    with pytest.raises(ValueError, match="至少需要 2 个字"):
        validate_answer("text", {"text": "你"}, {"min_length": 2}, None)


def test_validate_short_text_rejects_newline():
    validate_answer("short_text", {"text": "Alice"}, {"max_length": 16}, None)
    with pytest.raises(ValueError, match="换行"):
        validate_answer("short_text", {"text": "Ali\nce"}, None, None)
    # 多行文本不受换行限制
    validate_answer("text", {"text": "第一行\n第二行"}, None, None)


def test_validate_number_range_is_closed_interval():
    validate_answer("number", {"value": 1}, {"min_value": 1, "max_value": 10}, None)
    validate_answer("number", {"value": 10}, {"min_value": 1, "max_value": 10}, None)
    validate_answer("number", {"value": 3.5}, {"min_value": 1, "max_value": 10}, None)
    with pytest.raises(ValueError, match="不能大于 10"):
        validate_answer("number", {"value": 10.5}, {"min_value": 1, "max_value": 10}, None)
    with pytest.raises(ValueError, match="不能小于 1"):
        validate_answer("number", {"value": 0}, {"min_value": 1}, None)
    with pytest.raises(ValueError, match="数字"):
        validate_answer("number", {"value": "abc"}, None, None)


def test_validate_date_strict_format_and_range():
    validate_answer("date", {"value": "2026-08-06"}, {"min_date": "2026-01-01", "max_date": "2026-12-31"}, None)
    # 紧凑写法会被 date.fromisoformat 接受, 必须被正则挡掉
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        validate_answer("date", {"value": "20260806"}, None, None)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        validate_answer("date", {"value": "2026-02-30"}, None, None)
    with pytest.raises(ValueError, match="不能早于 2026-01-01"):
        validate_answer("date", {"value": "2025-12-31"}, {"min_date": "2026-01-01"}, None)
    with pytest.raises(ValueError, match="不能晚于 2026-12-31"):
        validate_answer("date", {"value": "2027-01-01"}, {"max_date": "2026-12-31"}, None)


def test_validate_rating_bounds_and_integer():
    validate_answer("rating", {"value": 1}, None, None)
    validate_answer("rating", {"value": 5}, None, None)  # 缺省满分 5
    with pytest.raises(ValueError, match="1 - 5 之间"):
        validate_answer("rating", {"value": 6}, None, None)
    with pytest.raises(ValueError, match="1 - 5 之间"):
        validate_answer("rating", {"value": 0}, None, None)
    validate_answer("rating", {"value": 7}, {"max_rating": 10}, None)
    with pytest.raises(ValueError, match="1 - 10 之间"):
        validate_answer("rating", {"value": 11}, {"max_rating": 10}, None)
    with pytest.raises(ValueError, match="整数"):
        validate_answer("rating", {"value": 3.5}, None, None)


def test_validate_image_count_limit():
    validate_answer("image", {"images": ["/uploads/1.jpg", "/uploads/2.jpg"]}, {"max_images": 2}, None)
    with pytest.raises(ValueError, match="最多上传 2 张"):
        validate_answer("image", {"images": ["/uploads/1.jpg", "/uploads/2.jpg", "/uploads/3.jpg"]}, {"max_images": 2}, None)
    # 缺省上限 5
    validate_answer("image", {"images": [f"/uploads/{i}.jpg" for i in range(5)]}, None, None)
    with pytest.raises(ValueError, match="最多上传 5 张"):
        validate_answer("image", {"images": [f"/uploads/{i}.jpg" for i in range(6)]}, None, None)


def test_validate_skips_empty_and_unrecognized_shapes():
    # 空内容一律放行: 必填与否由调用方判定
    validate_answer("rating", None, None, None)
    validate_answer("rating", {}, None, None)
    validate_answer("text", {"text": "   "}, {"min_length": 5}, None)
    validate_answer("multiple", {"values": []}, None, _OPTIONS)
    # 历史脏数据 / 未知题型不能被误报
    validate_answer("number", {"note": "历史脏数据"}, {"min_value": 100}, None)
    validate_answer("mystery", {"value": "x"}, None, None)


# ==================== is_answered ====================

def test_is_answered_counts_zero_and_false():
    assert is_answered("number", {"value": 0}) is True
    assert is_answered("boolean", {"value": False}) is True
    assert is_answered("rating", {"value": 0}) is True  # 合法性归 validate_answer 管
    assert is_answered("number", {"value": 0.0}) is True


def test_is_answered_empty_forms():
    assert is_answered("number", None) is False
    assert is_answered("number", {}) is False
    assert is_answered("text", {"text": ""}) is False
    assert is_answered("text", {"text": "   "}) is False
    assert is_answered("multiple", {"values": []}) is False
    assert is_answered("image", {"images": []}) is False
    assert is_answered("single", {"value": None}) is False


def test_is_answered_tolerates_legacy_flat_text():
    # 旧库里 text 题存成 {"value": "..."}, 不能被判成未作答
    assert is_answered("text", {"value": "历史扁平写法"}) is True
    assert is_answered("image", {"images": ["/uploads/a.jpg"]}) is True
    assert is_answered("multiple", {"values": ["A"]}) is True


# ==================== comparable_value ====================

def test_comparable_value_normalizes_boolean_to_lowercase():
    assert comparable_value("boolean", {"value": True}) == "true"
    assert comparable_value("boolean", {"value": False}) == "false"


def test_comparable_value_by_type():
    assert comparable_value("single", {"value": "A"}) == "A"
    assert comparable_value("multiple", {"values": ["A", "B"]}) == ["A", "B"]
    assert comparable_value("date", {"value": "2026-08-06"}) == "2026-08-06"
    assert comparable_value("number", {"value": 3.0}) == "3"
    assert comparable_value("number", {"value": 3.5}) == "3.5"
    assert comparable_value("rating", {"value": 4}) == "4"
    assert comparable_value("text", {"value": "  历史扁平  "}) == "历史扁平"
    assert comparable_value("text", {"text": ""}) is None
    assert comparable_value("single", None) is None


# ==================== answer_to_cell ====================

def test_answer_to_cell_maps_option_value_to_label():
    assert answer_to_cell("single", {"value": "A"}, _OPTIONS) == "选项A"
    assert answer_to_cell("select", {"value": "B"}, _OPTIONS) == "选项B"
    assert answer_to_cell("multiple", {"values": ["A", "B"]}, _OPTIONS) == "选项A, 选项B"
    # 选项被删改后映射不到, 原样输出 value, 不能吞成空白
    assert answer_to_cell("single", {"value": "Z"}, _OPTIONS) == "Z"
    assert answer_to_cell("single", {"value": "A"}, None) == "A"


def test_answer_to_cell_by_type():
    assert answer_to_cell("boolean", {"value": True}) == "是"
    assert answer_to_cell("boolean", {"value": False}) == "否"
    assert answer_to_cell("boolean", {"value": "false"}) == "否"  # 历史字符串布尔
    assert answer_to_cell("image", {"images": ["/uploads/a.jpg", "/uploads/b.jpg"]}) == "/uploads/a.jpg, /uploads/b.jpg"
    assert answer_to_cell("text", {"text": "hello"}) == "hello"
    assert answer_to_cell("text", {"value": "历史扁平"}) == "历史扁平"
    assert answer_to_cell("number", {"value": 3.0}) == "3"
    assert answer_to_cell("rating", {"value": 4}) == "4"
    assert answer_to_cell("text", None) == ""
    assert answer_to_cell("text", {}) == ""


# ==================== answer_scalar ====================

def test_answer_scalar_only_for_role_bindable_types():
    assert answer_scalar("short_text", {"text": "  Alice  "}) == "Alice"
    assert answer_scalar("text", {"text": "Alice"}) == "Alice"
    assert answer_scalar("single", {"value": "Alice"}) == "Alice"
    assert answer_scalar("select", {"value": "Alice"}) == "Alice"
    # 非绑定题型不能被抽成玩家名/QQ
    assert answer_scalar("number", {"value": "123"}) is None
    assert answer_scalar("multiple", {"values": ["Alice"]}) is None
    assert answer_scalar("image", {"images": ["/uploads/a.jpg"]}) is None
    assert answer_scalar("text", {"text": "   "}) is None
    assert answer_scalar("text", None) is None
