"""一次性迁移: 把 submissions.player_name 从 NOT NULL 改为可空 (支撑匿名收集表)。

SQLite 无法直接 ALTER DROP NOT NULL, 故整表重建: 按现有 schema 复制一张 player_name
可空的新表, 搬数据、重建索引、原子替换。幂等: 已可空则跳过。
注意: 重建后不再保留 DB 级外键(本项目 cascade 由 SQLAlchemy relationship 在应用层处理,
与既有 bot_notifications 等表一致)。运行前请先做一致性快照备份。

运行: python migrate_player_name_nullable.py
"""
from pathlib import Path
import sqlite3

DB_PATH = Path("data/survey.db")


def migrate() -> None:
    if not DB_PATH.exists():
        print(f"[跳过] 数据库不存在 ({DB_PATH})")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cols = cur.execute("PRAGMA table_info(submissions)").fetchall()  # (cid,name,type,notnull,dflt,pk)
    pn = next((c for c in cols if c[1] == "player_name"), None)
    if pn is None:
        print("[异常] submissions 无 player_name 列, 中止")
        conn.close()
        return
    if pn[3] == 0:  # notnull == 0
        print("[跳过] player_name 已是可空")
        conn.close()
        return

    col_names = [c[1] for c in cols]
    defs = []
    for _cid, name, ctype, notnull, dflt, pk in cols:
        d = f'"{name}" {ctype or ""}'.rstrip()
        if pk:
            d += " PRIMARY KEY"
        if notnull and name != "player_name":
            d += " NOT NULL"
        if dflt is not None:
            d += f" DEFAULT {dflt}"
        defs.append(d)
    create_sql = "CREATE TABLE submissions_new (\n  " + ",\n  ".join(defs) + "\n)"

    # 原索引 SQL (重建用; 指向 submissions 表名, 重命名后一致)
    idx_sql = [
        row[0]
        for row in cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='submissions' AND sql IS NOT NULL"
        ).fetchall()
    ]

    n_before = cur.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    collist = ", ".join(f'"{n}"' for n in col_names)

    cur.execute("PRAGMA foreign_keys=OFF")
    cur.execute("BEGIN")
    try:
        cur.execute(create_sql)
        cur.execute(f"INSERT INTO submissions_new ({collist}) SELECT {collist} FROM submissions")
        cur.execute("DROP TABLE submissions")
        cur.execute("ALTER TABLE submissions_new RENAME TO submissions")
        for s in idx_sql:
            cur.execute(s)
        conn.commit()
    except Exception:
        conn.rollback()
        cur.execute("PRAGMA foreign_keys=ON")
        conn.close()
        raise
    cur.execute("PRAGMA foreign_keys=ON")

    n_after = cur.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    pn2 = next(c for c in cur.execute("PRAGMA table_info(submissions)").fetchall() if c[1] == "player_name")
    conn.close()
    print(f"[完成] player_name 可空={pn2[3] == 0}; 行数 {n_before} -> {n_after}")
    if n_before != n_after:
        print("[警告] 行数不一致, 请立即核查/回滚!")


if __name__ == "__main__":
    migrate()
