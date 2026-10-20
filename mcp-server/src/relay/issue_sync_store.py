"""Small durable outbox for issue/Projects effects, independent of code runs."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


async def ensure_schema(db):
    # Works for existing databases as well as new installs. All migrations here
    # are additive; no conversation event can execute these external effects.
    await db.run("""CREATE TABLE IF NOT EXISTS issue_sync_streams (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL, sequence INTEGER NOT NULL,
        observation TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""")
    await db.run("""CREATE TABLE IF NOT EXISTS issue_sync_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        issue_key TEXT NOT NULL, sequence INTEGER NOT NULL, event_key TEXT UNIQUE NOT NULL,
        transition TEXT NOT NULL, actor TEXT NOT NULL, reconciled_by TEXT NOT NULL DEFAULT '', policy_digest TEXT NOT NULL,
        payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        read_attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        error TEXT NOT NULL DEFAULT '')""")
    await db.run("CREATE INDEX IF NOT EXISTS issue_sync_pending ON issue_sync_events(issue_key,state,id)")
    def migrate(conn):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(issue_sync_events)")}
        if "read_attempts" not in columns:
            conn.execute("ALTER TABLE issue_sync_events ADD COLUMN read_attempts INTEGER NOT NULL DEFAULT 0")
        if "reconciled_by" not in columns:
            conn.execute("ALTER TABLE issue_sync_events ADD COLUMN reconciled_by TEXT NOT NULL DEFAULT ''")
    await transaction(db, migrate)


def event(row):
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    return result


async def transaction(db, operation):
    def work():
        conn = db._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    return await db._in_thread(work)


async def record(db, cid, issue_key, fingerprint, observation, actions, actor, policy_digest, error=""):
    await ensure_schema(db)
    stamp = now()
    def work(conn):
        previous = conn.execute("SELECT fingerprint,sequence FROM issue_sync_streams WHERE conversation_id=?", (cid,)).fetchone()
        sequence = previous["sequence"] if previous else 0
        changed = not previous or previous["fingerprint"] != fingerprint
        if changed:
            sequence += 1
            # Uncertain effects must be reconciled; they cannot be silently
            # discarded just because a newer transition was observed.
            conn.execute("UPDATE issue_sync_events SET state='superseded', updated_at=? "
                         "WHERE conversation_id=? AND state IN ('pending','blocked')", (stamp, cid))
            for action in actions:
                key = digest([cid, sequence, action["target"]])
                conn.execute("INSERT OR IGNORE INTO issue_sync_events "
                             "(conversation_id,issue_key,sequence,event_key,transition,actor,policy_digest,payload,created_at,updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (cid, issue_key, sequence, key, observation["transition"], actor,
                              policy_digest, encoded(action), stamp, stamp))
        conn.execute("INSERT INTO issue_sync_streams(conversation_id,fingerprint,sequence,observation,error,updated_at) "
                     "VALUES (?,?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
                     "fingerprint=excluded.fingerprint,sequence=excluded.sequence,observation=excluded.observation,"
                     "error=excluded.error,updated_at=excluded.updated_at",
                     (cid, fingerprint, sequence, encoded(observation), error, stamp))
        return sequence
    return await transaction(db, work)


async def list_events(db, cid):
    await ensure_schema(db)
    return [event(row) for row in await db.run(
        "SELECT * FROM issue_sync_events WHERE conversation_id=? ORDER BY id DESC LIMIT 100", (cid,))]


async def observation(db, cid):
    await ensure_schema(db)
    rows = await db.run("SELECT * FROM issue_sync_streams WHERE conversation_id=?", (cid,))
    if not rows:
        return {}, ""
    return json.loads(rows[0]["observation"]), rows[0]["error"]


async def intent(db, cid):
    """Requires ensure_schema/observation first; empty marks explicit retry."""
    rows = await db.run("SELECT fingerprint,error FROM issue_sync_streams WHERE conversation_id=?", (cid,))
    return (rows[0]["fingerprint"], rows[0]["error"]) if rows else (None, "")


async def claim(db, cid, actor, *, reconcile_only=False):
    await ensure_schema(db)
    def work(conn):
        eligible = "state='uncertain' AND read_attempts<3" if reconcile_only else "state IN ('pending','uncertain') AND next_retry_at<=?"
        params = (cid, actor) if reconcile_only else (cid, actor, time.time())
        row = conn.execute("SELECT * FROM issue_sync_events e WHERE conversation_id=? AND (actor=? OR state='uncertain') AND " + eligible + " "
                           "AND NOT EXISTS(SELECT 1 FROM issue_sync_events active WHERE active.issue_key=e.issue_key "
                           "AND active.id<>e.id AND (active.state='processing' OR "
                           "(active.state='uncertain' AND active.id<e.id))) ORDER BY id LIMIT 1",
                           params).fetchone()
        if row is None:
            return None
        reconciled_by = actor if row["state"] == "uncertain" else row["reconciled_by"]
        conn.execute("UPDATE issue_sync_events SET state='processing',reconciled_by=?,updated_at=? WHERE id=?",
                     (reconciled_by, now(), row["id"]))
        result = event(row)
        result["reconciled_by"] = reconciled_by
        result["previous_state"] = result["state"]
        result["state"] = "processing"
        return result
    return await transaction(db, work)


async def attempt(db, event_id):
    rows = await db.run("UPDATE issue_sync_events SET attempts=attempts+1,updated_at=? "
                        "WHERE id=? AND state='processing' AND attempts<3 RETURNING attempts", (now(), event_id))
    return rows[0]["attempts"] if rows else None


async def finish(db, event_id, state, error="", delay=0):
    await db.run("UPDATE issue_sync_events SET state=?,error=?,updated_at=?,next_retry_at=? WHERE id=?",
                 (state, error, now(), time.time() + delay, event_id))


async def read_failure(db, event_id, error, uncertain=False):
    rows = await db.run("UPDATE issue_sync_events SET read_attempts=read_attempts+1 WHERE id=? RETURNING read_attempts", (event_id,))
    exhausted = rows[0]["read_attempts"] >= 3
    state = "uncertain" if uncertain else "blocked" if exhausted else "pending"
    # Exhausted uncertain observations remain visibly uncertain; an explicit
    # retry can resume reconciliation, but cannot clear the write attempt cap.
    if exhausted:
        error = "Límite de tres lecturas; reintento explícito necesario. " + error
    await finish(db, event_id, state, error, delay=(10 ** 10 if exhausted else 60))


async def retry(db, cid, actor, *, reset_error=None):
    def work(conn):
        # A confirmed retry may replan a stale precondition only when no write
        # was attempted. Never retire uncertainty or replenish a write budget.
        retired = conn.execute("UPDATE issue_sync_events SET state='superseded',updated_at=? "
                               "WHERE conversation_id=? AND actor=? AND state='blocked' AND attempts=0",
                               (now(), cid, actor)).rowcount
        conn.execute("UPDATE issue_sync_events SET read_attempts=0,next_retry_at=0,error='',updated_at=? "
                     "WHERE conversation_id=? AND state='uncertain'", (now(), cid))
        conn.execute("UPDATE issue_sync_events SET state='pending',read_attempts=0,next_retry_at=0,error='',updated_at=? "
                     "WHERE conversation_id=? AND actor=? AND state='blocked' AND attempts<3", (now(), cid, actor))
        if reset_error is not None:
            # An explicit user retry may acknowledge drift after a completed
            # transition. Never create a new intent over an active/uncertain
            # effect or reset an exhausted effect's attempt budget.
            conn.execute("UPDATE issue_sync_streams SET fingerprint='',error='',updated_at=? "
                         "WHERE conversation_id=? AND (error=? OR ?) AND NOT EXISTS "
                         "(SELECT 1 FROM issue_sync_events WHERE conversation_id=? AND state NOT IN ('applied','superseded')) "
                         "AND NOT EXISTS(SELECT 1 FROM issue_sync_events WHERE state IN ('processing','uncertain') "
                         "AND issue_key IN (SELECT issue_key FROM issue_sync_events WHERE conversation_id=?))",
                         (now(), cid, reset_error, retired > 0, cid, cid))
    await transaction(db, work)


async def recover(db):
    await ensure_schema(db)
    await db.run("UPDATE issue_sync_events SET state='uncertain',error=?,updated_at=? WHERE state='processing'",
                 ("Proceso interrumpido: reconciliar el resultado remoto antes de repetir", now()))
