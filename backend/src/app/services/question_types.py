"""题型注册表: 10 种题型的元信息, 以及"一条答案怎么读/怎么校验/怎么展示"的唯一实现。

本模块必须保持零依赖 (只用 stdlib): schemas.py 需要 QUESTION_TYPE_PATTERN 来约束题型字段,
若这里反向 import app 内任何模块会立刻成环。所有函数都是纯函数, 不碰 DB, 便于单测。
"""
import re
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class QuestionTypeSpec:
    """一种题型的元信息: 决定答案主键、能否绑定系统字段、能否做条件依赖、是否必须配选项。

    answerable=False 的是纯展示块 (分节标题), 它占一个题位、能配条件显示, 但不收答案 ——
    必填判定、内容校验、CSV 列与统计都必须把它排除在外。
    """
    type: str
    label: str
    content_key: str
    role_bindable: bool
    condition_source: bool
    needs_options: bool
    answerable: bool = True


# 字段顺序: type, label, content_key, role_bindable, condition_source, needs_options[, answerable]
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
        # 文件题: 收任意附件 (模型包/存档/日志), 答案存 [{"url","name","size"}]。
        # 与图片题同理不作条件依赖; 也不可绑系统字段 —— 答案不是标量。
        QuestionTypeSpec("file", "文件题", "files", False, False, False),
        # 分节说明块: 只渲染标题与说明, 不收答案。可以配条件显示 (按方向分支只亮出对应章节),
        # 但不能作为条件依赖题 —— 它没有答案可比。
        QuestionTypeSpec("section", "分节说明", "", False, False, False, answerable=False),
    )
}

QUESTION_TYPE_NAMES: tuple[str, ...] = tuple(QUESTION_TYPES)

# 会收上来答案的题型。必填判定/内容校验/CSV 列/统计一律以此为准, 而不是逐处硬写 != "section":
# 将来再加展示型题块 (分页符、富文本说明) 只需在注册表里标一次。
ANSWERABLE_TYPES: frozenset[str] = frozenset(t for t, s in QUESTION_TYPES.items() if s.answerable)


def is_answerable(qtype: str) -> bool:
    """该题型是否收答案。认不出的题型按收答案处理, 避免历史数据被静默跳过。"""
    spec = QUESTION_TYPES.get(qtype)
    return spec.answerable if spec else True

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
    # 文件题是平台化之后才加的, 库里不存在扁平写法的历史数据, 不给回退键
    "files": ("files",),
}
_UNKNOWN_FALLBACK: tuple[str, ...] = ("value", "text", "values", "images", "files")
_LIST_CONTENT_KEYS = ("values", "images", "files")

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 上传附件的合法地址形态: 只认本站 /uploads/ 下的单层文件名。
# 答案 content 是玩家直接 POST 上来的, 不卡形态就能伪造成 /uploads/../config.yml 或外站地址,
# 让审核端点开一个任意路径的下载链接。
_UPLOAD_URL_PATTERN = re.compile(r"^/uploads/[A-Za-z0-9._-]{1,128}$")

# 一条文件答案里 name 的展示上限; 超长文件名会撑爆审核列表, 且没有保留价值
_MAX_FILE_NAME_LENGTH = 255


def _raw_value(qtype: str, content: dict | None):
    """按题型取出答案原始值 (不做任何归一化), 取不到返回 None。

    回退键只有形态对得上才采信: 多选/图片回退到 "value" 时必须仍是数组, 否则宁可当未作答,
    免得把一个标量喂进列表分支。
    """
    if not isinstance(content, dict):
        return None
    spec = QUESTION_TYPES.get(qtype)
    # 展示型题块的 content_key 是空串, 取不到回退表; 认不出的题型同样走全量回退键
    keys = _CONTENT_FALLBACK.get(spec.content_key, _UNKNOWN_FALLBACK) if spec else _UNKNOWN_FALLBACK
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


def _file_entry_url(entry) -> str:
    """校验并取出一条文件答案的地址, 形态不对直接抛 ValueError (消息可展示给玩家)。

    ".." 必须单独排除: 它整串都由合法字符组成, 正则拦不住, 但拼进 uploads 目录就是父目录。
    """
    if not isinstance(entry, dict):
        raise ValueError("文件答案格式不正确, 请重新上传")
    url = entry.get("url")
    if not isinstance(url, str) or ".." in url or not _UPLOAD_URL_PATTERN.match(url):
        raise ValueError("文件地址不合法, 请重新上传")
    name = entry.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > _MAX_FILE_NAME_LENGTH):
        raise ValueError("文件名不合法, 请重命名后重试")
    return url


def _file_entry_text(entry) -> str:
    """把一条文件答案压成可展示文本: 优先原始文件名, 缺失时退回存储地址。"""
    if not isinstance(entry, dict):
        return _scalar_text(entry)
    name = entry.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    url = entry.get("url")
    return url if isinstance(url, str) else ""


def _normalized_extensions(raw) -> set[str]:
    """题目配置的扩展名白名单归一化为小写带点集合; 没配或配空返回空集 (即不限制)。"""
    if not isinstance(raw, list):
        return set()
    result = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        ext = item.strip().lower()
        if not ext:
            continue
        result.add(ext if ext.startswith(".") else f".{ext}")
    return result


def _url_extension(url: str) -> str:
    """取地址末段的扩展名 (小写带点); 无扩展名返回空串。"""
    tail = url.rsplit("/", 1)[-1]
    dot = tail.rfind(".")
    return tail[dot:].lower() if dot > 0 else ""


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
    if spec is None or not spec.answerable:
        return  # 展示型题块不收答案, 没有"内容合不合法"可言
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

    if qtype == "file":
        if not isinstance(raw, list):
            raise ValueError("文件答案格式不正确")
        max_files = _as_int(rules.get("max_files")) or 3
        if len(raw) > max_files:
            raise ValueError(f"最多上传 {max_files} 个文件")
        allowed = _normalized_extensions(rules.get("allowed_extensions"))
        for entry in raw:
            url = _file_entry_url(entry)
            # 白名单在上传端点已按站点配置卡过一道, 这里卡的是本题的更严要求 (如只收 .ysm)。
            # 认地址上的扩展名而不是 name: 落盘文件名才是审核端真正下载到的东西。
            if allowed and _url_extension(url) not in allowed:
                raise ValueError("文件类型不符合要求, 仅接受 " + " / ".join(sorted(allowed)))
        return


def attachment_urls(content: dict | None, key: str) -> list[str]:
    """取出答案里引用的上传地址; 只认 /uploads/ 下的合法单层路径, 其余条目跳过。

    key 传 "images" (图片题, 存字符串) 或 "files" (文件题, 存 {url,name,size} 对象)。
    清理任务与提交时的存在性校验共用这一处解析: 两边各写一份迟早在形态上分叉。
    """
    if not isinstance(content, dict):
        return []
    raw = content.get(key)
    if not isinstance(raw, list):
        return []
    urls: list[str] = []
    for entry in raw:
        url = entry.get("url") if isinstance(entry, dict) else entry
        if isinstance(url, str) and ".." not in url and _UPLOAD_URL_PATTERN.match(url):
            urls.append(url)
    return urls


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
        # 文件题的条目是对象, str(dict) 出来的是一坨 Python 字面量, 统一按文件名取文本
        if qtype == "file":
            return [_file_entry_text(item) for item in raw if _is_filled(item)]
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
    if qtype == "file":
        # 导出给人看的是原始文件名, 不是 uuid 存储名
        return ", ".join(_file_entry_text(item) for item in raw) if isinstance(raw, list) else ""
    labels = dict(_option_entries(options))
    if isinstance(raw, list):
        return ", ".join(labels.get(_scalar_text(item), _scalar_text(item)) for item in raw)
    text = _scalar_text(raw)
    return labels.get(text, text)
