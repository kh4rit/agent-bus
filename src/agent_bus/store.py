"""SQLite-backed mailbox: agents, messages, per-recipient deliveries and resource claims."""

from __future__ import annotations

import fnmatch
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}(@[A-Za-z0-9._-]{1,64})?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    login       TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER,
    reply_to    INTEGER,
    sender      TEXT NOT NULL,
    sender_login TEXT,
    addressed_to TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    message_id  INTEGER NOT NULL REFERENCES messages(id),
    agent       TEXT NOT NULL,
    read_at     REAL,
    PRIMARY KEY (message_id, agent)
);
CREATE INDEX IF NOT EXISTS deliveries_unread ON deliveries(agent, read_at);
CREATE TABLE IF NOT EXISTS claims (
    resource    TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    claimed_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session     TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""

ALIAS_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{5,160}$")

MAX_BODY = 64 * 1024


class BusError(ValueError):
    """A request the caller can fix (bad name, unknown thread, ...)."""


def validate_name(name: str) -> str:
    if not name or not NAME_RE.match(name):
        raise BusError(
            f"invalid agent name {name!r}: use letters, digits, '.', '_', '-', optionally 'tool@host' "
            "(e.g. 'claude@laptop')"
        )
    return name


def _is_pattern(s: str) -> bool:
    return any(c in s for c in "*?[")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)

    # -- agents ---------------------------------------------------------------

    def touch(self, name: str, login: str | None = None, description: str | None = None) -> None:
        validate_name(name)
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO agents(name, login, first_seen, last_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen, "
                "login=COALESCE(excluded.login, agents.login)",
                (name, login, now, now),
            )
            if description is not None:
                self._db.execute("UPDATE agents SET description=? WHERE name=?", (description[:2000], name))

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT a.name, a.description, a.login, a.last_seen, "
                "(SELECT COUNT(*) FROM deliveries d WHERE d.agent=a.name AND d.read_at IS NULL) AS unread "
                "FROM agents a ORDER BY a.last_seen DESC"
            ).fetchall()
        now = time.time()
        return [
            {
                "name": r["name"],
                "description": r["description"],
                "login": r["login"],
                "last_seen_ago_s": round(now - r["last_seen"]),
                "unread": r["unread"],
            }
            for r in rows
        ]

    # -- messages -------------------------------------------------------------

    def send(
        self,
        sender: str,
        to: list[str],
        body: str,
        subject: str = "",
        reply_to: int | None = None,
        sender_login: str | None = None,
    ) -> dict[str, Any]:
        validate_name(sender)
        if not body.strip():
            raise BusError("message body is empty")
        if len(body) > MAX_BODY:
            raise BusError(f"message body is {len(body)} chars; limit is {MAX_BODY}. Share a file path instead.")
        if not to:
            raise BusError("no recipients")

        with self._lock:
            known = [r["name"] for r in self._db.execute("SELECT name FROM agents")]
            recipients: list[str] = []
            unknown: list[str] = []
            for target in to:
                target = target.strip()
                if _is_pattern(target):
                    matched = [n for n in known if fnmatch.fnmatchcase(n, target) and n != sender]
                    if not matched:
                        raise BusError(f"pattern {target!r} matches no known agents; call list_agents")
                    recipients.extend(matched)
                else:
                    validate_name(target)
                    recipients.append(target)
                    if target not in known:
                        unknown.append(target)
            recipients = list(dict.fromkeys(recipients))

            thread_id = None
            if reply_to is not None:
                parent = self._db.execute("SELECT thread_id FROM messages WHERE id=?", (reply_to,)).fetchone()
                if parent is None:
                    raise BusError(f"reply_to message {reply_to} does not exist")
                thread_id = parent["thread_id"]

            self._db.execute("BEGIN")
            try:
                cur = self._db.execute(
                    "INSERT INTO messages(thread_id, reply_to, sender, sender_login, addressed_to, subject, body, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (thread_id, reply_to, sender, sender_login, ",".join(to), subject[:200], body, time.time()),
                )
                msg_id = cur.lastrowid
                if thread_id is None:
                    thread_id = msg_id
                    self._db.execute("UPDATE messages SET thread_id=? WHERE id=?", (msg_id, msg_id))
                self._db.executemany(
                    "INSERT OR IGNORE INTO deliveries(message_id, agent) VALUES (?, ?)",
                    [(msg_id, r) for r in recipients],
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

        result: dict[str, Any] = {"id": msg_id, "thread_id": thread_id, "delivered_to": recipients}
        if unknown:
            result["note"] = (
                f"{', '.join(unknown)} has never connected; the message will wait in their inbox until they do"
            )
        return result

    def inbox(
        self,
        agent: str,
        unread_only: bool = True,
        mark_read: bool = True,
        limit: int = 20,
        senders: list[str] | None = None,
        after_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Messages for `agent`, oldest first. `senders` (glob patterns) restricts which are returned/marked.

        With `after_id`, returns every message (read or not) with a larger id, oldest first, and marks nothing:
        a replayable cursor for consumers that must survive restarts.
        """
        validate_name(agent)
        limit = max(1, min(limit, 200))
        params: tuple = (agent,)
        if after_id is not None:
            where, order, mark_read = "d.agent=? AND m.id>?", "ASC", False
            params = (agent, after_id)
        elif unread_only:
            where, order = "d.agent=? AND d.read_at IS NULL", "ASC"
        else:
            where, order = "d.agent=?", "DESC"
        with self._lock:
            rows = self._db.execute(
                f"SELECT m.*, d.read_at FROM deliveries d JOIN messages m ON m.id=d.message_id "
                f"WHERE {where} ORDER BY m.id {order}",
                params,
            ).fetchall()
            if senders:
                rows = [r for r in rows if any(fnmatch.fnmatchcase(r["sender"], p) for p in senders)]
            rows = rows[:limit]
            if mark_read and rows:
                now = time.time()
                self._db.executemany(
                    "UPDATE deliveries SET read_at=? WHERE message_id=? AND agent=? AND read_at IS NULL",
                    [(now, r["id"], agent) for r in rows],
                )
        return [self._msg(r) for r in rows]

    def unread_count(self, agent: str, senders: list[str] | None = None) -> int:
        with self._lock:
            rows = self._db.execute(
                "SELECT m.sender FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "WHERE d.agent=? AND d.read_at IS NULL",
                (agent,),
            ).fetchall()
        if senders:
            rows = [r for r in rows if any(fnmatch.fnmatchcase(r["sender"], p) for p in senders)]
        return len(rows)

    def forget_agent(self, name: str, force: bool = False) -> dict[str, Any]:
        """Remove an agent from the directory (and its claims). Refuses if it has unread mail unless forced.
        Message history stays; the agent reappears if it connects again."""
        with self._lock:
            if self._db.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone() is None:
                raise BusError(f"no agent named {name!r}")
            unread = self._db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE agent=? AND read_at IS NULL", (name,)
            ).fetchone()[0]
            if unread and not force:
                raise BusError(f"{name} has {unread} unread message(s); pass force to drop them")
            self._db.execute("DELETE FROM deliveries WHERE agent=? AND read_at IS NULL", (name,))
            self._db.execute("DELETE FROM claims WHERE agent=?", (name,))
            self._db.execute("DELETE FROM agents WHERE name=?", (name,))
        return {"forgotten": name, "dropped_unread": unread}

    def thread(self, thread_id: int, viewer: str) -> list[dict[str, Any]]:
        """Whole thread, if `viewer` sent or received any message in it."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (thread_id,)).fetchall()
            if not rows:
                raise BusError(f"thread {thread_id} does not exist")
            involved = self._db.execute(
                "SELECT 1 FROM messages m LEFT JOIN deliveries d ON d.message_id=m.id "
                "WHERE m.thread_id=? AND (m.sender=? OR d.agent=?) LIMIT 1",
                (thread_id, viewer, viewer),
            ).fetchone()
        if not involved:
            raise BusError(f"{viewer} is not part of thread {thread_id}")
        return [self._msg(r) for r in rows]

    @staticmethod
    def _msg(r: sqlite3.Row) -> dict[str, Any]:
        out = {
            "id": r["id"],
            "thread_id": r["thread_id"],
            "from": r["sender"],
            "to": r["addressed_to"],
            "subject": r["subject"],
            "body": r["body"],
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["created_at"])),
        }
        if r["reply_to"] is not None:
            out["reply_to"] = r["reply_to"]
        if r["sender_login"]:
            out["from_login"] = r["sender_login"]
        return out

    # -- aliases ----------------------------------------------------------------
    # A Claude session's MCP connection may not know its project (Claude Code runs the headersHelper from
    # ~/.claude, sometimes before the session has a project), but its hooks do. Both sides can compute the
    # same process key ('<host>:<claude pid>:<start time>'); the hooks register key -> agent name, and MCP
    # requests carrying that key act as that agent.

    def set_session_alias(self, session: str, agent: str) -> None:
        validate_name(agent)
        if not ALIAS_KEY_RE.match(session):
            raise BusError(f"invalid alias key {session!r}")
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions(session, agent, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session) DO UPDATE SET agent=excluded.agent, updated_at=excluded.updated_at",
                (session, agent, time.time()),
            )

    def session_alias(self, session: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT agent FROM sessions WHERE session=?", (session,)).fetchone()
        return row["agent"] if row else None

    # -- claims ---------------------------------------------------------------

    def _expire_claims(self) -> None:
        self._db.execute("DELETE FROM claims WHERE expires_at < ?", (time.time(),))

    def claim(self, agent: str, resource: str, ttl_minutes: float = 60, note: str = "") -> dict[str, Any]:
        resource = resource.strip()
        if not resource:
            raise BusError("resource is empty")
        ttl_minutes = max(1, min(ttl_minutes, 24 * 60))
        now = time.time()
        with self._lock:
            self._expire_claims()
            row = self._db.execute("SELECT * FROM claims WHERE resource=?", (resource,)).fetchone()
            if row is not None and row["agent"] != agent:
                return {
                    "granted": False,
                    "held_by": row["agent"],
                    "note": row["note"],
                    "expires_in_min": round((row["expires_at"] - now) / 60, 1),
                }
            self._db.execute(
                "INSERT INTO claims(resource, agent, note, claimed_at, expires_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(resource) DO UPDATE SET note=excluded.note, expires_at=excluded.expires_at",
                (resource, agent, note[:500], now, now + ttl_minutes * 60),
            )
        return {"granted": True, "resource": resource, "expires_in_min": ttl_minutes}

    def release(self, agent: str, resource: str) -> dict[str, Any]:
        with self._lock:
            cur = self._db.execute("DELETE FROM claims WHERE resource=? AND agent=?", (resource.strip(), agent))
        return {"released": cur.rowcount > 0}

    def list_claims(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._expire_claims()
            rows = self._db.execute("SELECT * FROM claims ORDER BY claimed_at").fetchall()
        return [
            {
                "resource": r["resource"],
                "agent": r["agent"],
                "note": r["note"],
                "expires_in_min": round((r["expires_at"] - now) / 60, 1),
            }
            for r in rows
        ]
