"""player_name 整表重建迁移的回归测试。

构造一张旧 schema(player_name NOT NULL)的 submissions, 跑迁移后断言:
可空生效、数据与索引保留、能插 NULL、幂等。删掉重建逻辑这些断言必须挂掉。
"""
import sys
import sqlite3
import importlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ 根, 供导入迁移脚本


def _seed_old_schema(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE submissions (
            id INTEGER PRIMARY KEY,
            survey_id INTEGER NOT NULL,
            player_name VARCHAR(64) NOT NULL,
            qq VARCHAR(20),
            status VARCHAR(20) DEFAULT 'pending',
            token VARCHAR(43),
            created_at DATETIME
        );
        CREATE INDEX ix_submissions_player_name ON submissions (player_name);
        CREATE UNIQUE INDEX ix_submissions_token ON submissions (token);
        INSERT INTO submissions (id, survey_id, player_name, qq, status, token) VALUES
            (1, 1, 'Alice', '123', 'approved', 'tok1'),
            (2, 1, 'Bob', NULL, 'pending', 'tok2');
        """
    )
    conn.commit()
    conn.close()


def test_player_name_nullable_rebuild(tmp_path):
    db = tmp_path / "survey.db"
    _seed_old_schema(db)

    mod = importlib.import_module("migrate_player_name_nullable")
    mod.DB_PATH = Path(db)
    mod.migrate()

    conn = sqlite3.connect(db)
    info = {c[1]: c for c in conn.execute("PRAGMA table_info(submissions)")}
    assert info["player_name"][3] == 0, "player_name 应变为可空"
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 2, "行数应保留"
    assert conn.execute("SELECT player_name FROM submissions WHERE id=1").fetchone()[0] == "Alice"
    assert conn.execute("SELECT status FROM submissions WHERE id=2").fetchone()[0] == "pending"

    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='submissions'"
    )}
    assert "ix_submissions_token" in idx and "ix_submissions_player_name" in idx, "索引应重建"

    # 现在可插入匿名(NULL 名字)提交
    conn.execute("INSERT INTO submissions (id, survey_id, player_name) VALUES (3, 1, NULL)")
    conn.commit()
    assert conn.execute("SELECT player_name FROM submissions WHERE id=3").fetchone()[0] is None
    conn.close()

    # 幂等: 再跑跳过, 不动数据
    mod.migrate()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 3
    conn.close()
