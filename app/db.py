"""SQLite 连接管理与表结构初始化。

并发安全策略：
- 启用 WAL + foreign_keys；
- 所有写事务使用 BEGIN IMMEDIATE，配合进程内可重入锁，将写操作串行化；
- recognitions 上对活跃状态（approved / pending）的两列各建唯一部分索引，
  在数据库层兜底“一份待复核/已认记录独占源课与要求”，杜绝重复使用。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get("DB_PATH", "/data/credits.db"))

# 单 worker 下的进程内写锁：读多写少场景下，BEGIN IMMEDIATE 已足够，
# 锁用于让锁等待在应用层排队并减少 SQLITE_BUSY。
_write_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None,  # 手工管理事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    student_id  TEXT    NOT NULL,
    course_code TEXT    NOT NULL,
    credits     INTEGER NOT NULL CHECK (credits > 0),
    score       INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    PRIMARY KEY (student_id, course_code)
);

CREATE TABLE IF NOT EXISTS requirements (
    student_id      TEXT    NOT NULL,
    requirement_code TEXT   NOT NULL,
    required_credits INTEGER NOT NULL CHECK (required_credits > 0),
    PRIMARY KEY (student_id, requirement_code)
);

CREATE TABLE IF NOT EXISTS substitutions (
    student_id       TEXT    NOT NULL,
    course_code      TEXT    NOT NULL,
    requirement_code TEXT    NOT NULL,
    min_score        INTEGER NOT NULL CHECK (min_score BETWEEN 0 AND 100),
    PRIMARY KEY (student_id, course_code, requirement_code),
    FOREIGN KEY (student_id, course_code)
        REFERENCES courses (student_id, course_code),
    FOREIGN KEY (student_id, requirement_code)
        REFERENCES requirements (student_id, requirement_code)
);

CREATE TABLE IF NOT EXISTS recognitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id       TEXT    NOT NULL,
    course_code      TEXT    NOT NULL,
    requirement_code TEXT    NOT NULL,
    status           TEXT    NOT NULL CHECK (status IN ('approved', 'pending')),
    reason           TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (student_id, course_code)
        REFERENCES courses (student_id, course_code),
    FOREIGN KEY (student_id, requirement_code)
        REFERENCES requirements (student_id, requirement_code)
);

-- 活跃记录（已认 + 待复核）对源课、要求的独占占用。
CREATE UNIQUE INDEX IF NOT EXISTS idx_recog_active_course
    ON recognitions (student_id, course_code)
    WHERE status IN ('approved', 'pending');
CREATE UNIQUE INDEX IF NOT EXISTS idx_recog_active_requirement
    ON recognitions (student_id, requirement_code)
    WHERE status IN ('approved', 'pending');
"""


def init_db() -> None:
    """创建数据目录与全部表/索引。容器启动时执行；已存在则为空操作。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """串行化的立即写事务：整批 SQL 要么全部提交，要么回滚。"""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
