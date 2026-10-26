"""Transactional SQLite persistence with a checkpoint on every state change."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .schema import WorkflowError


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        # Never follow a repository-supplied .workflow or state.db symlink.
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise WorkflowError("Каталог состояния .workflow или state.db не должен быть символьной ссылкой.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise WorkflowError("Каталог состояния .workflow или state.db изменён на ссылку.")
        self.connection = sqlite3.connect(str(self.path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    version INTEGER NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (run_id, version)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    version INTEGER NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def close(self) -> None:
        self.connection.close()

    def create(self, state: dict[str, Any]) -> None:
        state["version"] = 1
        data = json.dumps(state, ensure_ascii=False)
        with self.connection:
            self.connection.execute("INSERT INTO runs(id,version,state) VALUES(?,?,?)", (state["id"], 1, data))
            self.connection.execute("INSERT INTO checkpoints(run_id,version,state) VALUES(?,?,?)", (state["id"], 1, data))
            self.connection.execute("INSERT INTO events(run_id,version,action,details) VALUES(?,?,?,?)",
                                    (state["id"], 1, "created", "{}"))

    def get(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"Неизвестная задача: {run_id}")
        return json.loads(row["state"])

    def save(self, state: dict[str, Any], action: str, details: dict[str, Any] | None = None) -> None:
        previous_version = state["version"]
        next_version = previous_version + 1
        new_state = {**state, "version": next_version}
        data = json.dumps(new_state, ensure_ascii=False)
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE runs SET version=?,state=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND version=?",
                (next_version, data, state["id"], previous_version),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("Состояние изменилось в другом процессе; загрузите его заново.")
            self.connection.execute("INSERT INTO checkpoints(run_id,version,state) VALUES(?,?,?)",
                                    (state["id"], next_version, data))
            self.connection.execute("INSERT INTO events(run_id,version,action,details) VALUES(?,?,?,?)",
                                    (state["id"], next_version, action,
                                     json.dumps(details or {}, ensure_ascii=False)))
        state["version"] = next_version

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT id,state FROM runs ORDER BY updated_at DESC").fetchall()
        return [{"id": row["id"], "status": json.loads(row["state"])["status"]} for row in rows]

    def history(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT version,action,details,created_at FROM events WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(version=row["version"], action=row["action"],
                     details=json.loads(row["details"]), at=row["created_at"]) for row in rows]

    def checkpoints(self, run_id: str) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS n FROM checkpoints WHERE run_id=?", (run_id,)).fetchone()
        return int(row["n"])
