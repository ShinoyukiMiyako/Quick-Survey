"""一次性迁移: 把 questions.condition.depends_on 从"按 id 升序的位置下标"重映射为"依赖题 question_id"。

背景:
    分支题的 condition.depends_on 旧语义是"该 survey 的题目按 id 升序排列后的 0-based 下标",
    与前端一致。管理端过去每次保存"删光重建"会重排 id 使下标恰好对齐, 掩盖了这一脆弱设计。
    改为增量 diff 更新后 id 不再重排, 故统一把 depends_on 定义为依赖题的 question_id(稳定引用)。
    本脚本把存量数据从"下标"转成"question_id"。

只在"存量旧库"执行一次:
    - 新建的库(代码已是 id 语义)不要跑本脚本, 否则会把 id 误当下标二次映射。
    - 已执行记录写入 _migrations 表, 重复运行自动跳过。
    - 默认 dry-run 仅预览; 确认无误后加 --apply 落库。

运行:
    python migrate_condition_depends_on_to_id.py            # 预览(不改库)
    python migrate_condition_depends_on_to_id.py --apply    # 落库
"""
import sys
import json
import sqlite3
from pathlib import Path

MIGRATION_KEY = "condition_depends_on_index_to_id"
DB_PATH = Path("data/survey.db")


def migrate(apply: bool) -> None:
    if not DB_PATH.exists():
        print(f"[跳过] 数据库文件不存在 ({DB_PATH}), 无需迁移")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 幂等记录表: 防止在同一库上二次映射
    cur.execute(
        "CREATE TABLE IF NOT EXISTS _migrations ("
        "key TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute("SELECT 1 FROM _migrations WHERE key = ?", (MIGRATION_KEY,))
    if cur.fetchone():
        print(f"[跳过] 迁移 {MIGRATION_KEY} 已执行过, 不重复")
        conn.close()
        return

    cur.execute("SELECT id FROM surveys ORDER BY id ASC")
    survey_ids = [r[0] for r in cur.fetchall()]

    remapped = 0
    out_of_bound = 0
    pending_updates = []  # (question_id, new_condition_json)

    for sid in survey_ids:
        # 按 id 升序 == 旧 depends_on 的下标基准
        cur.execute(
            "SELECT id, condition FROM questions WHERE survey_id = ? ORDER BY id ASC",
            (sid,),
        )
        rows = cur.fetchall()
        ordered_ids = [r[0] for r in rows]

        for qid, cond_raw in rows:
            if not cond_raw:
                continue
            try:
                cond = json.loads(cond_raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(cond, dict):
                continue
            dep = cond.get("depends_on")
            if not isinstance(dep, int):
                continue

            # dep 作为旧下标; 越界通常意味着数据已是 id 语义或异常, 保守跳过并告警
            if dep < 0 or dep >= len(ordered_ids):
                print(
                    f"[告警] survey={sid} question={qid} depends_on={dep} "
                    f"越界(该卷题数 {len(ordered_ids)}), 跳过 — 若本库已是 id 语义属正常"
                )
                out_of_bound += 1
                continue

            new_dep = ordered_ids[dep]
            cond["depends_on"] = new_dep
            pending_updates.append((qid, json.dumps(cond, ensure_ascii=False)))
            remapped += 1
            print(f"[改] survey={sid} question={qid}: depends_on {dep} -> {new_dep}")

    if not apply:
        print(
            f"\n[预览] 将重映射 {remapped} 条 condition, 越界跳过 {out_of_bound} 条。"
            f" 确认无误后加 --apply 落库。"
        )
        conn.close()
        return

    for qid, cond_json in pending_updates:
        cur.execute("UPDATE questions SET condition = ? WHERE id = ?", (cond_json, qid))
    cur.execute("INSERT INTO _migrations (key) VALUES (?)", (MIGRATION_KEY,))
    conn.commit()
    conn.close()
    print(f"\n[完成] 已重映射 {remapped} 条 condition, 越界跳过 {out_of_bound} 条。")


if __name__ == "__main__":
    migrate(apply="--apply" in sys.argv[1:])
