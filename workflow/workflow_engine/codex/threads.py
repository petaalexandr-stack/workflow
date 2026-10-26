"""Durable Codex thread identities and fail-closed pending turns."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator
from pathlib import Path
from typing import Any

from ..schema import WorkflowError


class ThreadStore:
    def __init__(self, db: Path):
        db = Path(db)
        if db.is_symlink() or db.parent.is_symlink():
            raise WorkflowError("Codex thread store не должен быть символьной ссылкой")
        db.parent.mkdir(parents=True, exist_ok=True)
        self.path = db
        with self._connect() as cx:
            cx.execute("""CREATE TABLE IF NOT EXISTS codex_threads (
                run_id TEXT NOT NULL, node_id TEXT NOT NULL, role TEXT NOT NULL,
                thread_id TEXT NOT NULL, title TEXT NOT NULL, pending INTEGER NOT NULL DEFAULT 0,
                last_turn_id TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(run_id,node_id,role), UNIQUE(thread_id))""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(self.path, timeout=30)
        cx.row_factory = sqlite3.Row
        cx.execute("PRAGMA busy_timeout=30000")
        try:
            yield cx
            cx.commit()
        except Exception:
            cx.rollback()
            raise
        finally:
            cx.close()

    def lookup(self, run_id: str, node_id: str, role: str) -> dict[str, Any] | None:
        with self._connect() as cx:
            row = cx.execute("SELECT * FROM codex_threads WHERE run_id=? AND node_id=? AND role=?",
                             (run_id, node_id, role)).fetchone()
        return dict(row) if row else None

    def register(self, run_id: str, node_id: str, role: str, thread_id: str, title: str) -> None:
        with self._connect() as cx:
            cx.execute("INSERT INTO codex_threads(run_id,node_id,role,thread_id,title) VALUES(?,?,?,?,?)",
                       (run_id, node_id, role, thread_id, title))

    def mark_pending(self, run_id: str, node_id: str, role: str) -> None:
        with self._connect() as cx:
            cursor = cx.execute("""UPDATE codex_threads SET pending=1,updated_at=CURRENT_TIMESTAMP
                WHERE run_id=? AND node_id=? AND role=? AND pending=0""", (run_id, node_id, role))
            if cursor.rowcount != 1:
                raise WorkflowError("Чат Codex занят или ход остался неопределённым после сбоя. "
                                    "Проверьте чат и выполните codex-clear-pending вручную.")

    def completed(self, run_id: str, node_id: str, role: str, turn_id: str) -> None:
        with self._connect() as cx:
            cx.execute("""UPDATE codex_threads SET pending=0,last_turn_id=?,updated_at=CURRENT_TIMESTAMP
                WHERE run_id=? AND node_id=? AND role=?""", (turn_id, run_id, node_id, role))

    def clear_pending(self, run_id: str, node_id: str, role: str) -> None:
        if not self.lookup(run_id, node_id, role):
            raise WorkflowError("Чат с такой ролью и этапом отсутствует")
        with self._connect() as cx:
            cx.execute("UPDATE codex_threads SET pending=0 WHERE run_id=? AND node_id=? AND role=?",
                       (run_id, node_id, role))

    def list_threads(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as cx:
            rows = cx.execute("SELECT * FROM codex_threads WHERE run_id=? ORDER BY node_id,role",
                              (run_id,)).fetchall()
        return [dict(row) for row in rows]
