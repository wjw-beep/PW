from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class MemoryBudgetExceeded(RuntimeError):
    pass


class ConcurrentWriteError(RuntimeError):
    pass


class SQLiteMemory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY CHECK(id = 1), payload TEXT NOT NULL)")

    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def load(self) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT payload FROM memory WHERE id = 1").fetchone()
        if row is None:
            return {
                "schema_version": 1,
                "revision": 0,
                "evidence": {},
                "evidence_history": [],
                "coverage": [],
                "obligations": {},
                "source_stats": {},
                "decisions": {},
                "trace": [],
                "last_cycle": None,
            }
        state = json.loads(row[0])
        if state.get("schema_version") != 1:
            raise ValueError("Unsupported memory schema")
        return state

    @staticmethod
    def approximate_tokens(state: dict, chars_per_token: float) -> int:
        return int((len(json.dumps(state, ensure_ascii=False, separators=(",", ":"))) + chars_per_token - 1) / chars_per_token)

    def save(self, state: dict, settings: dict) -> int:
        limits = [
            ("evidence_history", "max_history_records"),
            ("coverage", "max_coverage_records"),
            ("trace", "max_trace_records"),
        ]
        for field, setting in limits:
            maximum = int(settings[setting])
            state[field] = state[field][-maximum:] if maximum > 0 else []
        factor = float(settings["chars_per_token"])
        ceiling = int(settings["memory_tokens"])
        usage = self.approximate_tokens(state, factor)
        while usage > ceiling and any(state[field] for field, _ in limits):
            for field, _ in limits:
                if state[field]:
                    state[field].pop(0)
                    break
            usage = self.approximate_tokens(state, factor)
        if usage > ceiling:
            raise MemoryBudgetExceeded(f"Essential state requires {usage} tokens, budget is {ceiling}")
        expected = int(state.get("revision", 0))
        state["revision"] = expected + 1
        serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM memory WHERE id = 1").fetchone()
            actual = int(json.loads(row[0]).get("revision", 0)) if row else 0
            if actual != expected:
                state["revision"] = expected
                raise ConcurrentWriteError(f"Concurrent state modification: expected revision {expected}, found {actual}")
            connection.execute("INSERT INTO memory(id, payload) VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET payload = excluded.payload", (serialized,))
        return self.approximate_tokens(state, factor)
