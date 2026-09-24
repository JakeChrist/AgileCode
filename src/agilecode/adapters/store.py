"""One SQLite database per project, owned by a single database worker thread."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, TypeVar

from agilecode.core.types import AgileCodeError, now, uid

T = TypeVar("T")
SCHEMA_VERSION = 1
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY, data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS discussions(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, session_id TEXT UNIQUE NOT NULL,
 archived INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets(
 id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('Backlog','Ready','In Progress','Done')),
 team_ref TEXT NOT NULL, process_id TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
 position REAL NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
 parent_id TEXT REFERENCES tickets(id), merge_auto INTEGER, revision INTEGER NOT NULL DEFAULT 1,
 created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discussion_tickets(
 discussion_id TEXT NOT NULL REFERENCES discussions(id), ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
 created TEXT NOT NULL, UNIQUE(discussion_id,ticket_id));
CREATE TABLE IF NOT EXISTS dependencies(
 ticket_id TEXT NOT NULL REFERENCES tickets(id), depends_on TEXT NOT NULL REFERENCES tickets(id),
 PRIMARY KEY(ticket_id,depends_on), CHECK(ticket_id<>depends_on));
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), snapshot TEXT NOT NULL,
 state TEXT NOT NULL, step_index INTEGER NOT NULL DEFAULT 0, outputs TEXT NOT NULL DEFAULT '{}',
 corrections INTEGER NOT NULL DEFAULT 0, correction TEXT, workspace TEXT NOT NULL, branch TEXT NOT NULL,
 base_branch TEXT NOT NULL, base_oid TEXT NOT NULL, final_oid TEXT, final_tree TEXT,
 error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(ticket_id)
 WHERE state NOT IN ('completed','cancelled','failed','integrated');
CREATE TABLE IF NOT EXISTS attempts(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), step_id TEXT NOT NULL,
 number INTEGER NOT NULL, session_id TEXT UNIQUE NOT NULL, member TEXT NOT NULL, model TEXT NOT NULL,
 status TEXT NOT NULL, result TEXT, fingerprint TEXT, phase TEXT NOT NULL DEFAULT 'development',
 context TEXT NOT NULL, created TEXT NOT NULL, finished TEXT,
 UNIQUE(run_id,step_id,number,phase));
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL,
 request_id TEXT, partial INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,
 UNIQUE(session_id,seq));
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,seq);
CREATE TABLE IF NOT EXISTS requests(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, model TEXT NOT NULL, response_model TEXT,
 input TEXT NOT NULL, status TEXT NOT NULL, usage TEXT, error TEXT, created TEXT NOT NULL, finished TEXT);
CREATE TABLE IF NOT EXISTS tools(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES requests(id),
 call_id TEXT NOT NULL, name TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL,
 result TEXT, category TEXT NOT NULL, created TEXT NOT NULL, finished TEXT,
 UNIQUE(request_id,call_id));
CREATE TABLE IF NOT EXISTS waits(
 id TEXT PRIMARY KEY, tool_id TEXT NOT NULL REFERENCES tools(id), ticket_id TEXT NOT NULL REFERENCES tickets(id),
 session_id TEXT NOT NULL, kind TEXT NOT NULL, prompt TEXT NOT NULL, binding TEXT NOT NULL,
 status TEXT NOT NULL, answer TEXT, created TEXT NOT NULL, resolved TEXT);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), run_id TEXT REFERENCES runs(id),
 attempt_id TEXT REFERENCES attempts(id), command TEXT NOT NULL, cwd TEXT NOT NULL,
 env_hash TEXT, status TEXT NOT NULL, exit_code INTEGER, artifact_id TEXT,
 background INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, finished TEXT);
CREATE TABLE IF NOT EXISTS artifacts(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), name TEXT NOT NULL,
 path TEXT UNIQUE NOT NULL, size INTEGER NOT NULL DEFAULT 0, hash TEXT, complete INTEGER NOT NULL DEFAULT 0,
 created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id), requirement TEXT NOT NULL,
 status TEXT NOT NULL, method TEXT NOT NULL, source_tool TEXT REFERENCES tools(id),
 fingerprint TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), run_id TEXT NOT NULL REFERENCES runs(id),
 ref TEXT UNIQUE NOT NULL, oid TEXT, tree TEXT, label TEXT NOT NULL, status TEXT NOT NULL,
 step_index INTEGER NOT NULL, outputs TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS integrations(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), run_id TEXT NOT NULL REFERENCES runs(id),
 target_branch TEXT NOT NULL, target_oid TEXT NOT NULL, ticket_oid TEXT NOT NULL, candidate_oid TEXT,
 tree TEXT, workspace TEXT NOT NULL, state TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}',
 auto INTEGER NOT NULL DEFAULT 0, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_id TEXT, data TEXT NOT NULL, status TEXT NOT NULL,
 result TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, kind TEXT NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL);
PRAGMA user_version=1;
"""
TABLES = {"meta", "settings", "teams", "discussions", "tickets", "dependencies", "runs", "attempts", "messages",
          "requests", "tools", "waits", "jobs", "artifacts", "evidence", "checkpoints", "integrations", "operations", "activity"}


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def decode(value: str | None, default=None):
    return json.loads(value) if value else default


class ProjectStore:
    def __init__(self, path: Path):
        self.path = path
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agilecode-db")
        self.conn: sqlite3.Connection | None = None
        self.closed = False

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        def initialize():
            self.conn = sqlite3.connect(self.path, timeout=10)
            self.conn.row_factory = sqlite3.Row
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                self.conn.close()
                self.conn = None
                raise AgileCodeError(f"Database schema {version} is newer than this application. Nothing was modified.")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA busy_timeout=10000")
            self.conn.executescript(SCHEMA)
        await self.call(initialize)

    async def call(self, fn: Callable[[], T]) -> T:
        if self.closed:
            raise AgileCodeError("The project database is closed.")
        return await asyncio.get_running_loop().run_in_executor(self.worker, fn)

    async def transaction(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def execute():
            assert self.conn is not None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self.conn)
                self.conn.commit()
                return result
            except BaseException:
                self.conn.rollback()
                raise
        return await self.call(execute)

    async def query(self, sql: str, parameters=()) -> list[dict[str, Any]]:
        def read():
            assert self.conn is not None
            return [dict(row) for row in self.conn.execute(sql, parameters).fetchall()]
        return await self.call(read)

    async def one(self, sql: str, parameters=()) -> dict[str, Any] | None:
        rows = await self.query(sql, parameters)
        return rows[0] if rows else None

    async def execute(self, sql: str, parameters=()) -> int:
        return await self.transaction(lambda c: c.execute(sql, parameters).rowcount)

    async def get(self, table: str, identity: str) -> dict[str, Any]:
        if table not in TABLES:
            raise ValueError("Unknown table")
        result = await self.one(f"SELECT * FROM {table} WHERE id=?", (identity,))
        if result is None:
            raise AgileCodeError(f"Unknown {table} ID: {identity}")
        return result

    async def insert(self, table: str, data: dict[str, Any]) -> None:
        if table not in TABLES or any(not k.replace('_', '').isalnum() for k in data):
            raise ValueError("Unknown table or column")
        keys = list(data)
        await self.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(data.values()))

    async def update(self, table: str, identity: str, **fields) -> None:
        if table not in TABLES or any(not k.replace('_', '').isalnum() for k in fields):
            raise ValueError("Unknown table or column")
        await self.execute(f"UPDATE {table} SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), identity))

    async def message(self, session_id: str, body: dict, request_id: str | None = None, partial=False) -> str:
        identity = uid()
        def append(c):
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]
            c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", (identity, session_id, seq, encode(body), request_id, int(partial), now()))
        await self.transaction(append)
        return identity

    async def messages(self, session_id: str, include_partial=False) -> list[dict]:
        rows = await self.query("SELECT * FROM messages WHERE session_id=?" + ("" if include_partial else " AND partial=0") + " ORDER BY seq", (session_id,))
        return [{**r, "body": decode(r["body"])} for r in rows]

    async def activity(self, kind: str, data: dict, ticket_id: str | None = None):
        await self.execute("INSERT INTO activity(ticket_id,kind,data,created) VALUES(?,?,?,?)", (ticket_id, kind, encode(data), now()))

    async def backup(self, destination: Path) -> None:
        def copy():
            assert self.conn is not None
            target = sqlite3.connect(destination)
            try:
                self.conn.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AgileCodeError("Backup integrity check failed.")
            finally:
                target.close()
        await self.call(copy)

    async def close(self) -> None:
        if not self.closed:
            def close_connection():
                if self.conn:
                    self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self.conn.close()
            await self.call(close_connection)
            self.closed = True
            self.worker.shutdown(wait=True)
