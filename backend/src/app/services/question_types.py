"""题型注册表: 10 种题型的元信息, 以及"一条答案怎么读/怎么校验/怎么展示"的唯一实现。

本模块必须保持零依赖 (只用 stdlib): schemas.py 需要 QUESTION_TYPE_PATTERN 来约束题型字段,
若这里反向 import app 内任何模块会立刻成环。所有函数都是纯函数, 不碰 DB, 便于单测。
"""
import re
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class QuestionTypeSpec:
    """一种题型的元信息: 决定答案主键、能否绑定系统字段、能否做条件依赖、是否必须配选项。"""
    type: str
    label: str
    content_key: str
    role_bindable: bool
    condition_source: bool
    needs_options: bool


# 字段顺序: type, label, content_key, role_bindable, condition_source, needs_options
QUESTION_TYPES: dict[str, QuestionTypeSpec] = {
    spec.type: spec
    for spec in (
        QuestionTypeSpec("single", "单选题", "value", True, True, True),
        QuestionTypeSpec("select", "下拉单选", "value", True, True, True),
        QuestionTypeSpec("multiple", "多选题", "values", False, True, True),
        QuestionTypeSpec("boolean", "判断题", "value", False, True, False),
        QuestionTypeSpec("text", "多行文本", "text", True, True, False),
        QuestionTypeSpec("short_text", "单行文本", "text", True, True, False),
        QuestionTypeSpec("number", "数字题", "value", False, True, False),
        QuestionTypeSpec("date", "日期题", "value", False, True, False),
        QuestionTypeSpec("rating", "评分题", "value", False, True, False),
        # 图片不参与条件比较: 路径字符串对玩家无语义, 拿来做分支只会误判
        QuestionTypeSpec("image", "图片题", "images", False, False, False),
    )
}

QUESTION_TYPE_NAMES: tuple[str, ...] = tuple(QUESTION_TYPES)

# 供 pydantic Field(pattern=...) 直接使用; 由注册表生成, 新增题型时不会漏改校验
QUESTION_TYPE_PATTERN: str = "^(" + "|".join(QUESTION_TYPE_NAMES) + ")$"


# ==================== 内部工具 ====================

# content 主键取不到时的回退键: 存量库里 text 题写成 {"value": "..."} 的历史扁平数据还在,
# 直接判未答会让老提交在导出/条件/统计里整片消失。
_CONTENT_FALLBACK: dict[str, tuple[str, ...]] = {
    "value": ("value", "text"),
    "text": ("text", "value"),
    "values": ("values", "value"),
    "images": ("images", "value"),
}
_UNKNOWN_FALLBACK: tuple[str, ...] = ("value", "text", "values", "images")
_LIST_CONTENT_KEYS = ("values", "images")

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _raw_value(qtype: str, content: dict | None):
    """按题型取出答案原始值 (不做任何归一化), 取不到返回 None。

    回退键只有形态对得上才采信: 多选/图片回退到 "value" 时必须仍是数组, 否则宁可当未作答,
    免得把一个标量喂进列表分支。
    """
    if not isinstance(content, dict):
        return None
    spec = QUESTION_TYPES.get(qtype)
    keys = _CONTENT_FALLBACK[spec.content_key] if spec else _UNKNOWN_FALLBACK
    expects_list = spec is not None and spec.content_key in _LIST_CONTENT_KEYS
    for index, key in enumerate(keys):
        value = content.get(key)
        if value is None:
            continue
        if index > 0 and expects_list and not isinstance(value, list):
            continue
        return value
    return None


def _is_filled(value) -> bool:
    """标量/数组是否算"填了东西"。bool 与数字 (含 False / 0) 一律算填了。"""
    if value is None:
        return False
    if isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return any(_is_filled(item) for item in value)
    return True


def _fmt_num(value) -> str:
    """整数值去掉浮点尾巴: JSON 里 3 与 3.0 是同一个答案, 展示与条件比较都不该出现 "3.0"。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _scalar_text(value) -> str:
    """标量归一化为可比较/可展示的字符串。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _fmt_num(value)
    return str(value).strip()


def _as_float(value) -> float | None:
    """validation JSON 里的数字可能是 null / 空串 / 字符串数字, 取不到就视为未配置该限制。

    bool 单独排除: 它是 int 子类, float(True) 会静默变成 1.0。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    """只接受"整数值": 4 / 4.0 / "4" 可以, 3.5 / "abc" 不行。"""
    number = _as_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _parse_iso_date(value) -> date | None:
    """严格 YYYY-MM-DD 解析, 不合法返回 None。

    不能只靠 date.fromisoformat: Python 3.11 起它还接受 "20260806" 等紧凑写法, 与前端
    input[type=date] 的形态不一致, 会让库里混进一批彼此不可比的日期串。
    """
    if not isinstance(value, str) or not _DATE_PATTERN.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _option_entries(options: list | None) -> list[tuple[str, str]]:
    """选项列表归一化为 (value, label) 对; 兼容历史里直接存字符串数组的写法。"""
    entries: list[tuple[str, str]] = []
    for opt in options or []:
        if isinstance(opt, dict):
            value = opt.get("value")
            if value is None:
                continue
            label = opt.get("label")
            entries.append((str(value), str(value) if label is None else str(label)))
        elif isinstance(opt, str):
            entries.append((opt, opt))
    return entries


def _boolean_cell(raw) -> str:
    """判断题历史上存过 True/False 也存过 "true"/"false" 字符串, 两种都要认。"""
    if raw is True:
        return "是"
    if raw is False:
        return "否"
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered == "true":
            return "是"
        if lowered == "false":
            return "否"
    return ""


# ==================== 对外接口 ====================

def is_answered(qtype: str, content: dict | None) -> bool:
    """判定一条答案是否算"已作答"。

    number 的 0 与 boolean 的 False 是有效答案, 不能用 `if not content` 一刀切,
    否则必填校验会把它们误判为未填。
    """
    return _is_filled(_raw_value(qtype, content))


def validate_answer(qtype: str, content: dict | None, validation: dict | None, options: list | None) -> None:
    """校验"已填内容"是否合法, 违规抛 ValueError (消息可直接展示给玩家), 由 API 层转 400。

    必填与否不在这里判定: 内容为空直接放行, 调用方按 is_answered + is_required 自行决定。
    未知题型与不认识的 content 形态同样放行 —— 存量数据形态不一, 宁可漏判也不能把老数据卡死。
    """
    spec = QUESTION_TYPES.get(qtype)
    if spec is None:
        return
    raw = _raw_value(qtype, content)
    if not _is_filled(raw):
        return

    rules = validation or {}

    if qtype in ("single", "select"):
        allowed = {value for value, _ in _option_entries(options)}
        # 题目没配选项时无从校验, 放行, 交由管理端去修题
        if allowed and _scalar_text(raw) not in allowed:
            raise ValueError("所选选项不在可选范围内")
        return

    if qtype == "multiple":
        if not isinstance(raw, list):
            raise ValueError("多选题答案格式不正确")
        allowed = {value for value, _ in _option_entries(options)}
        if allowed:
            for item in raw:
                if _scalar_text(item) not in allowed:
                    raise ValueError("所选选项不在可选范围内")
        return

    if qtype == "boolean":
        if not isinstance(raw, bool):
            raise ValueError("判断题只能选择是或否")
        return

    if qtype in ("text", "short_text"):
        if not isinstance(raw, str):
            raise ValueError("该题需要填写文字")
        if qtype == "short_text" and ("\n" in raw or "\r" in raw):
            raise ValueError("单行文本不能包含换行")
        # 首尾空白不计入字数, 否则玩家敲几个空格就能凑够下限
        length = len(raw.strip())
        min_length = _as_int(rules.get("min_length"))
        if min_length is not None and length < min_length:
            raise ValueError(f"内容至少需要 {min_length} 个字")
        max_length = _as_int(rules.get("max_length"))
        if max_length is not None and length > max_length:
            raise ValueError(f"内容最多 {max_length} 个字")
        return

    if qtype == "number":
        number = _as_float(raw)
        if number is None:
            raise ValueError("请填写数字")
        min_value = _as_float(rules.get("min_value"))
        if min_value is not None and number < min_value:
            raise ValueError(f"数值不能小于 {_fmt_num(min_value)}")
        max_value = _as_float(rules.get("max_value"))
        if max_value is not None and number > max_value:
            raise ValueError(f"数值不能大于 {_fmt_num(max_value)}")
        return

    if qtype == "date":
        picked = _parse_iso_date(raw)
        if picked is None:
            raise ValueError("日期格式需为 YYYY-MM-DD")
        min_date = _parse_iso_date(rules.get("min_date"))
        if min_date is not None and picked < min_date:
            raise ValueError(f"日期不能早于 {min_date.isoformat()}")
        max_date = _parse_iso_date(rules.get("max_date"))
        if max_date is not None and picked > max_date:
            raise ValueError(f"日期不能晚于 {max_date.isoformat()}")
        return

    if qtype == "rating":
        score = _as_int(raw)
        if score is None:
            raise ValueError("评分必须是整数")
        max_rating = _as_int(rules.get("max_rating")) or 5
        if score < 1 or score > max_rating:
            raise ValueError(f"评分需在 1 - {max_rating} 之间")
        return

    if qtype == "image":
        if not isinstance(raw, list):
            raise ValueError("图片答案格式不正确")
        max_images = _as_int(rules.get("max_images")) or 5
        if len(raw) > max_images:
            raise ValueError(f"最多上传 {max_images} 张图片")
        return


def answer_scalar(qtype: str, content: dict | None) -> str | None:
    """抽取可绑定 player_name/qq 的标量答案; 非绑定题型一律 None, 避免把多选/图片塞进系统字段。"""
    spec = QUESTION_TYPES.get(qtype)
    if spec is None or not spec.role_bindable:
        return None
    raw = _raw_value(qtype, content)
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def comparable_value(qtype: str, content: dict | None) -> str | list[str] | None:
    """供条件引擎比较的归一化值; 未作答返回 None。

    boolean 必须归一成小写 "true"/"false": 历史实现直接 str(True) 得到 "True", 与前端
    show_when 里写的 "true" 永远不相等, 判断题的分支因此从来不显示。
    """
    raw = _raw_value(qtype, content)
    if not _is_filled(raw):
        return None
    if isinstance(raw, list):
        return [_scalar_text(item) for item in raw if _is_filled(item)]
    return _scalar_text(raw)


def answer_to_cell(qtype: str, content: dict | None, options: list | None = None) -> str:
    """把一条答案压成 CSV 单元格文本。

    选项题把存的 value 映射回 label (导出给人看, value 往往是无意义的短码);
    选项被删改后映射不到就原样输出 value, 不能把历史答案吞成空白。
    """
    raw = _raw_value(qtype, content)
    if raw is None:
        return ""
    if qtype == "boolean":
        return _boolean_cell(raw)
    labels = dict(_option_entries(options))
    if isinstance(raw, list):
        return ", ".join(labels.get(_scalar_text(item), _scalar_text(item)) for item in raw)
    text = _scalar_text(raw)
    return labels.get(text, text)
