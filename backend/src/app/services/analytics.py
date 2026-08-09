"""
问卷统计聚合。

答案存在 JSON 列里, SQLite 无法在 SQL 侧对其做分组统计, 因此把提交与答案一次性取回 Python
侧算完: selectinload 让"取提交"与"取答案"各走一条 SQL, 避免逐条提交再查答案的 N+1。
问卷可能有上万条提交, 但每次聚合只发 3 条 SQL (题目 + 提交 + 答案)。
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Question, Submission
from app.services import question_types

# 面板固定展示这三个状态, 即使当前一条都没有也要给出 0, 否则前端条形图会缺档
STATUS_KEYS = ("pending", "approved", "rejected")

# 趋势图窗口 (含今天), 更久远的提交只计入总量不进趋势
DAILY_WINDOW_DAYS = 30

# 文本题样例: 只取最近几条, 每条截断, 避免把长篇作文塞进统计响应
SAMPLE_LIMIT = 5
SAMPLE_MAX_CHARS = 80

DISTRIBUTION_TYPES = ("single", "select", "multiple", "boolean")
NUMERIC_TYPES = ("number", "rating")
SAMPLE_TYPES = ("text", "short_text")

BOOLEAN_CHOICES = (("true", "是"), ("false", "否"))


def _utc_date(dt: Optional[datetime]) -> Optional[date]:
    """取提交时间的 UTC 日期。

    SQLite 取回的是 naive(按 UTC 存), 而同一会话里刚创建的对象仍带 tzinfo, 两者混用会算错日期,
    故统一先归到 UTC 再取 date。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.date()


def _as_float(value) -> Optional[float]:
    """把答案里的数值压成 float。

    非数字 (bool / 空串 / 历史脏数据) 返回 None 而不是抛: 整份统计报表不该因为单条烂数据出不来。
    bool 单独挡掉 —— Python 里 True 是 int 的子类, 不挡会让判断题混进数字统计。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _declared_choices(qtype: str, options) -> list[tuple[str, str]]:
    """题目声明过的候选项 [(value, label)]。判断题没有 options, 用固定的是/否。"""
    if qtype == "boolean":
        return list(BOOLEAN_CHOICES)

    choices: list[tuple[str, str]] = []
    for opt in (options or []):
        if not isinstance(opt, dict):
            continue
        value = opt.get("value")
        if value is None:
            continue
        label = opt.get("label")
        choices.append((str(value), str(label) if label else str(value)))
    return choices


def _build_distribution(qtype: str, options, contents: list, answered: int) -> list[dict]:
    """选项题的分布。

    计数键复用 question_types.comparable_value, 与条件引擎用同一套归一化 (判断题为 "true"/"false"),
    否则统计口径会和分支逻辑对不上。
    """
    counter: dict[str, int] = {}
    for content in contents:
        key = question_types.comparable_value(qtype, content)
        if key is None:
            continue
        if isinstance(key, list):
            for item in key:
                counter[item] = counter.get(item, 0) + 1
        else:
            counter[key] = counter.get(key, 0) + 1

    rows: list[dict] = []
    declared: set[str] = set()
    # 声明过的选项即使 0 票也保留一行, 面板才看得出"这个选项无人选择"
    for value, label in _declared_choices(qtype, options):
        declared.add(value)
        rows.append({"value": value, "label": label, "count": counter.get(value, 0)})
    # 选项被改名/删除后残留的历史答案原样列出, 不能悄悄吞掉真实票数
    for value, count in counter.items():
        if value not in declared:
            rows.append({"value": value, "label": value, "count": count})

    for row in rows:
        row["percent"] = round(row["count"] / answered * 100, 1) if answered else 0.0
    return rows


def _build_numeric(contents: list) -> Optional[dict]:
    """数字/评分题的极值与均值; 一条有效数值都没有时返回 None (前端据此显示占位)。"""
    values: list[float] = []
    for content in contents:
        value = _as_float((content or {}).get("value"))
        if value is not None:
            values.append(value)
    if not values:
        return None
    return {
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / len(values), 1),
    }


def _build_samples(contents: list) -> list[str]:
    """文本题样例: contents 按提交时间升序传入, 逆序遍历即"最近优先"。"""
    samples: list[str] = []
    for content in reversed(contents):
        text = (content or {}).get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        samples.append(text[:SAMPLE_MAX_CHARS])
        if len(samples) >= SAMPLE_LIMIT:
            break
    return samples


async def build_survey_analytics(db: AsyncSession, survey) -> dict:
    """聚合某问卷的统计报表 (总量/状态分布/平均耗时/每日趋势/逐题分布)。"""
    # 不依赖调用方是否 selectinload 过 survey.questions: 异步下访问未预加载的关系会抛 MissingGreenlet
    question_result = await db.execute(
        select(Question)
        .where(Question.survey_id == survey.id)
        .order_by(Question.order.asc(), Question.id.asc())
    )
    # 分节说明块不收答案, 统计里给它出一行"0 份作答"只会让人以为漏数据了
    questions = [q for q in question_result.scalars().all() if question_types.is_answerable(q.type)]

    submission_result = await db.execute(
        select(Submission)
        .options(selectinload(Submission.answers))
        .where(Submission.survey_id == survey.id)
        .order_by(Submission.created_at.asc(), Submission.id.asc())
    )
    submissions = list(submission_result.scalars().all())

    by_status = {key: 0 for key in STATUS_KEYS}
    daily_counter: dict[date, int] = {}
    durations: list[float] = []
    # 升序追加, 使每题的答案序列与提交时间同序, 样例取"最近"时直接逆序即可
    contents_by_question: dict[int, list] = {q.id: [] for q in questions}

    for submission in submissions:
        by_status[submission.status] = by_status.get(submission.status, 0) + 1

        if submission.fill_duration is not None:
            durations.append(submission.fill_duration)

        day = _utc_date(submission.created_at)
        if day is not None:
            daily_counter[day] = daily_counter.get(day, 0) + 1

        for answer in submission.answers:
            bucket = contents_by_question.get(answer.question_id)
            if bucket is None:
                continue  # 题已删 / 答案不属本卷, 不参与聚合
            bucket.append(answer.content)

    earliest = datetime.now(timezone.utc).date() - timedelta(days=DAILY_WINDOW_DAYS - 1)
    daily = [
        {"date": day.isoformat(), "count": count}
        for day, count in sorted(daily_counter.items())
        if day >= earliest
    ]

    question_rows: list[dict] = []
    for question in questions:
        contents = contents_by_question[question.id]
        answered = sum(1 for c in contents if question_types.is_answered(question.type, c))
        question_rows.append({
            "question_id": question.id,
            "title": question.title,
            "type": question.type,
            "answered": answered,
            "distribution": (
                _build_distribution(question.type, question.options, contents, answered)
                if question.type in DISTRIBUTION_TYPES else []
            ),
            "numeric": _build_numeric(contents) if question.type in NUMERIC_TYPES else None,
            "samples": _build_samples(contents) if question.type in SAMPLE_TYPES else [],
        })

    return {
        "survey_id": survey.id,
        "title": survey.title,
        "total_submissions": len(submissions),
        "by_status": by_status,
        "avg_fill_duration": round(sum(durations) / len(durations), 1) if durations else None,
        "daily": daily,
        "questions": question_rows,
    }
