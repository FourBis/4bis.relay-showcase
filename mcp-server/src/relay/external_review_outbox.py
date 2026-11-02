"""Durable, at-most-one-POST intents for external review/evidence publication."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from .issue_sync_store import digest, encoded, now, transaction


def ensure_schema_conn(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS external_review_effects (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, correlation_id TEXT NOT NULL, head_sha TEXT NOT NULL,
        actor TEXT NOT NULL, payload TEXT NOT NULL, payload_digest TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        read_attempts INTEGER NOT NULL DEFAULT 0, publisher TEXT NOT NULL DEFAULT '{}',
        remote TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
        claim_token TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS external_review_pending ON external_review_effects(conversation_id,state)")
    if "claim_token" not in {row["name"] for row in conn.execute("PRAGMA table_info(external_review_effects)")}:
        conn.execute("ALTER TABLE external_review_effects ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''")


async def ensure_schema(db):
    await transaction(db, ensure_schema_conn)


def decode(row):
    result = dict(row)
    for key in ("payload", "publisher", "remote"):
        result[key] = json.loads(result[key])
    return result


def enqueue_in_transaction(conn, cid, kind, correlation_id, sha, payload, actor):
    ensure_schema_conn(conn)
    key = digest([cid, kind, correlation_id, sha, actor])
    fingerprint, stamp = digest(payload), now()
    previous = conn.execute("SELECT * FROM external_review_effects WHERE id=?", (key,)).fetchone()
    if previous:
        if previous["payload_digest"] != fingerprint:
            raise ValueError("La correlación de publicación ya contiene otro contenido o destino")
        return decode(previous)
    conn.execute("INSERT INTO external_review_effects "
                 "(id,conversation_id,kind,correlation_id,head_sha,actor,payload,payload_digest,created_at,updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (key, cid, kind, correlation_id, sha, actor,
                 encoded(payload), fingerprint, stamp, stamp))
    return decode(conn.execute("SELECT * FROM external_review_effects WHERE id=?", (key,)).fetchone())


async def rows(db, cid):
    await ensure_schema(db)
    latest = await db.run("SELECT * FROM external_review_effects WHERE conversation_id=? "
                          "ORDER BY created_at DESC,id DESC LIMIT 100", (cid,))
    return [decode(row) for row in reversed(latest)]


async def claim(db, cid):
    await ensure_schema(db)
    def work(conn):
        # A live worker owns processing. Only startup recovery may retire it.
        if conn.execute("SELECT 1 FROM external_review_effects WHERE conversation_id=? AND state='processing'", (cid,)).fetchone():
            return None
        row = conn.execute("SELECT * FROM external_review_effects WHERE conversation_id=? "
                           "AND ((state='pending' AND attempts<3) OR (state='blocked' AND attempts=0) "
                           "OR (state='uncertain' AND read_attempts<3)) "
                           "ORDER BY CASE state WHEN 'pending' THEN 0 WHEN 'uncertain' THEN 1 ELSE 2 END,updated_at,id LIMIT 1", (cid,)).fetchone()
        if row is None:
            return None
        result = decode(row)
        result["previous_state"] = row["state"]
        result["claim_token"] = uuid.uuid4().hex
        conn.execute("UPDATE external_review_effects SET state='processing',claim_token=?,updated_at=? WHERE id=?",
                     (result["claim_token"], now(), row["id"]))
        return result
    return await transaction(db, work)


async def admit_attempt(db, event, publisher, authorize, *, admission):
    """Reserve a POST only with fresh authorization in the same write transaction.

    ``authorize`` is synchronous and must use this connection exclusively. The
    commit receipt survives cancellation of the await: db._in_thread waits for
    the worker to close before propagating cancellation to the publisher.
    """
    def work():
        conn = db._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM external_review_effects WHERE id=?", (event["id"],)).fetchone()
            if (not row or row["claim_token"] != event["claim_token"]
                    or row["state"] != "processing" or not 0 <= row["attempts"] < 3):
                conn.rollback()
                return False
            current = decode(row)
            immutable = ("id", "conversation_id", "kind", "correlation_id", "head_sha", "actor", "payload_digest", "attempts")
            if (any(current[key] != event[key] for key in immutable)
                    or digest(current["payload"]) != current["payload_digest"]
                    or digest(event["payload"]) != current["payload_digest"]
                    or digest(current["publisher"]) != digest(publisher)):
                raise ValueError("La intención de publicación cambió antes de reservar el intento")
            authorize(conn, current)
            changed = conn.execute("UPDATE external_review_effects SET attempts=attempts+1,publisher=?,updated_at=? "
                                   "WHERE id=? AND claim_token=? AND state='processing' AND attempts=? AND attempts<3 RETURNING id",
                                   (encoded(publisher), now(), current["id"], event["claim_token"], current["attempts"])).fetchone()
            if not changed:
                conn.rollback()
                return False
            conn.commit()
            admission["committed"] = True
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    return await db._in_thread(work)


async def bind_publisher(db, key, publisher, claim_token):
    await db.run("UPDATE external_review_effects SET publisher=?,updated_at=? WHERE id=? AND claim_token=? AND state='processing'",
                 (encoded(publisher), now(), key, claim_token))


async def finish(db, key, state, *, claim_token, error="", remote=None):
    await db.run("UPDATE external_review_effects SET state=?,error=?,remote=?,updated_at=? WHERE id=? AND claim_token=?",
                 (state, error[:1000], encoded(remote or {}), now(), key, claim_token))


async def observation_failed(db, key, claim_token):
    await db.run("UPDATE external_review_effects SET read_attempts=read_attempts+1,state='uncertain',"
                 "error=?,updated_at=? WHERE id=? AND claim_token=?",
                 ("Resultado remoto sin confirmar; no se volverá a publicar automáticamente", now(), key, claim_token))


async def recover(db):
    """Call once at startup, before workers: a lost process never replays POST."""
    await ensure_schema(db)
    await db.run("UPDATE external_review_effects SET state=CASE WHEN attempts>0 THEN 'uncertain' ELSE 'pending' END,claim_token='',"
                 "error=?,updated_at=? WHERE state='processing'",
                 ("Publicación interrumpida; reconciliar el resultado remoto", now()))


async def defer_group(db, cid, actor, *, blocked=False):
    """Rotate the group while retaining its effects' relative scheduling order."""
    def work(conn):
        rows = conn.execute("SELECT id FROM external_review_effects WHERE conversation_id=? AND actor=? "
                            "AND ((state='pending' AND attempts<3) OR (state='blocked' AND attempts=0) "
                            "OR (state='uncertain' AND read_attempts<3)) ORDER BY updated_at,id", (cid, actor)).fetchall()
        stamp = datetime.fromisoformat(now())
        for index, row in enumerate(rows):
            # Equal group timestamps would turn claim's id tie-breaker into
            # permanent priority. Keep waiting peers ahead of the effect just
            # serviced, with every timestamp still recent for group rotation.
            scheduled = stamp - timedelta(microseconds=len(rows) - index - 1)
            conn.execute("UPDATE external_review_effects SET updated_at=?,"
                         "state=CASE WHEN ? AND attempts=0 AND state IN ('pending','blocked') THEN 'blocked' ELSE state END,"
                         "error=CASE WHEN ? AND state<>'uncertain' THEN ? ELSE error END WHERE id=?",
                         (scheduled.isoformat(), blocked, blocked,
                          "Publicación bloqueada: verifica acceso, proyecto y configuración", row["id"]))
    await transaction(db, work)


async def retry(db, cid):
    # Rejected means a complete HTTP rejection, not a missing response. A
    # human may retry at most three POSTs, each with fresh guards. Uncertain
    # effects only replenish observation reads, never their POST permission.
    await ensure_schema(db)
    await db.run("UPDATE external_review_effects SET state='pending',error='',updated_at=? "
                 "WHERE conversation_id=? AND state='rejected' AND attempts<3", (now(), cid))
    await db.run("UPDATE external_review_effects SET read_attempts=0,error='',updated_at=? "
                 "WHERE conversation_id=? AND state='uncertain'", (now(), cid))
