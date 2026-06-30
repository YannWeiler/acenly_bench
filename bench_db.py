"""
ACENLY Bench — SQLite storage for benchmark results.
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "bench.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS benchmarks (
                id               TEXT PRIMARY KEY,
                created_at       TEXT NOT NULL,
                commit_hash      TEXT NOT NULL,
                branch           TEXT,
                function_name    TEXT NOT NULL,
                file_path        TEXT NOT NULL,
                median_ms        REAL NOT NULL,
                p95_ms           REAL,
                min_ms           REAL,
                max_ms           REAL,
                trials           INTEGER,
                speedup_vs_prev  REAL,
                python_version   TEXT
            );
        """)


def save_benchmark(
    *,
    function_name: str,
    file_path: str,
    commit_hash: str,
    branch: str,
    median_ms: float,
    p95_ms: Optional[float] = None,
    min_ms: Optional[float] = None,
    max_ms: Optional[float] = None,
    trials: Optional[int] = None,
    speedup_vs_prev: Optional[float] = None,
    python_version: Optional[str] = None,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO benchmarks
               (id, created_at, commit_hash, branch, function_name, file_path,
                median_ms, p95_ms, min_ms, max_ms, trials, speedup_vs_prev, python_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                datetime.now(timezone.utc).isoformat(),
                commit_hash, branch, function_name, file_path,
                median_ms, p95_ms, min_ms, max_ms, trials,
                speedup_vs_prev, python_version,
            ),
        )


def get_benchmark_last(function_name: str, file_path: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM benchmarks
               WHERE function_name = ? AND file_path = ?
               ORDER BY created_at DESC LIMIT 1""",
            (function_name, file_path),
        ).fetchone()
    return dict(row) if row else None


def get_benchmark_history(function_name: str, file_path: str, limit: int = 20) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM benchmarks
               WHERE function_name = ? AND file_path = ?
               ORDER BY created_at DESC LIMIT ?""",
            (function_name, file_path, limit),
        ).fetchall()
    return [dict(r) for r in rows]
