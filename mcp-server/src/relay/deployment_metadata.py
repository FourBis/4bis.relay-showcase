"""Explicit local deployment receipts; startup and health never allocate numbers."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from aiohttp import web

from . import __version__

RUNTIME_KEY = web.AppKey("deployment_runtime", dict)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS relay_deployment_attempts ("
    "deployment_id TEXT PRIMARY KEY, commit_sha TEXT NOT NULL, software_version TEXT NOT NULL,"
    "prepared_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','confirmed','failed')),"
    "expected_active_id TEXT, number INTEGER UNIQUE CHECK(number>0), deployed_at TEXT, runtime_pid INTEGER,"
    "rollback_of TEXT, target_deployment_id TEXT)",
    "CREATE TABLE IF NOT EXISTS relay_deployment_active ("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), deployment_id TEXT NOT NULL)",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def default_db_path():
    # Unlike config.db_path(), readers must not create even a directory.
    return Path(os.environ.get("FOURBIS_DB_PATH") or Path.home() / ".4bis" / "relay.db").expanduser()


def _validate(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} inválido")
    return value


def _attempt(conn, deployment_id):
    row = conn.execute("SELECT * FROM relay_deployment_attempts WHERE deployment_id=?", (deployment_id,)).fetchone()
    return dict(row) if row else None


def _active_id(conn):
    row = conn.execute("SELECT deployment_id FROM relay_deployment_active WHERE singleton=1").fetchone()
    return row[0] if row else None


def _write(path, operation):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=15, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            result = operation(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def history(path):
    """Read-only, including for a missing database or an older Relay schema."""
    path = Path(path)
    if not path.is_file():
        return {"active": None, "attempts": []}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")  # Active receipt and history come from one read snapshot.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='relay_deployment_attempts'").fetchone():
            return {"active": None, "attempts": []}
        active_id = _active_id(conn)
        return {"active": _attempt(conn, active_id), "attempts": [dict(row) for row in conn.execute(
            "SELECT * FROM relay_deployment_attempts ORDER BY rowid")]}


def prepare(path, deployment_id, commit_sha, software_version, *, rollback_of=None, target_deployment_id=None):
    _validate(deployment_id, _ID, "deployment_id")
    _validate(commit_sha, _SHA, "commit_sha")
    _validate(software_version, _VERSION, "software_version")
    if bool(rollback_of) != bool(target_deployment_id):
        raise ValueError("Un rollback requiere rollback_of y target_deployment_id")
    for value in (rollback_of, target_deployment_id):
        if value is not None:
            _validate(value, _ID, "deployment_id de rollback")

    def operation(conn):
        existing = _attempt(conn, deployment_id)
        if existing:
            wanted = (commit_sha, software_version, rollback_of, target_deployment_id)
            if tuple(existing[key] for key in ("commit_sha", "software_version", "rollback_of", "target_deployment_id")) != wanted:
                raise ValueError("deployment_id ya usado con otro contenido")
            if existing["status"] == "failed":
                raise ValueError("Un intento fallido es terminal; usa un deployment_id nuevo")
            return existing
        if rollback_of:
            source, target = _attempt(conn, rollback_of), _attempt(conn, target_deployment_id)
            if not source or source["status"] not in {"failed", "confirmed"} or not target or target["status"] != "confirmed":
                raise ValueError("El rollback requiere un origen fallido o confirmado y un destino confirmado anterior")
            active_id = _active_id(conn)
            if source["status"] == "confirmed":
                if active_id != rollback_of or target["number"] >= source["number"]:
                    raise ValueError("Sólo se revierte el despliegue activo hacia un número anterior")
            elif active_id != source["expected_active_id"] or target_deployment_id != active_id:
                raise ValueError("El activo cambió desde el intento fallido o no coincide con el destino de recuperación")
            if (target["commit_sha"], target["software_version"]) != (commit_sha, software_version):
                raise ValueError("El código del rollback no coincide con el destino confirmado")
        conn.execute("INSERT INTO relay_deployment_attempts "
                     "(deployment_id,commit_sha,software_version,prepared_at,status,expected_active_id,rollback_of,target_deployment_id) "
                     "VALUES (?,?,?,?,'pending',?,?,?)", (deployment_id, commit_sha, software_version, now(),
                                                          _active_id(conn), rollback_of, target_deployment_id))
        return _attempt(conn, deployment_id)
    return _write(path, operation)


def confirm(path, deployment_id, runtime_commit_sha, runtime_software_version, runtime_pid, runtime_deployment_id):
    if type(runtime_pid) is not int or runtime_pid <= 0:
        raise ValueError("runtime_pid debe identificar el proceso comprobado")

    def operation(conn):
        item = _attempt(conn, deployment_id)
        if not item or item["status"] == "failed":
            raise ValueError("El intento no está pendiente o ya falló")
        if (runtime_commit_sha, runtime_software_version, runtime_deployment_id) != (
                item["commit_sha"], item["software_version"], item["deployment_id"]):
            raise ValueError("La identidad del proceso no coincide con el intento preparado")
        if item["status"] == "confirmed":
            if item["runtime_pid"] != runtime_pid:
                raise ValueError("Este deployment_id ya fue confirmado por otro proceso")
            return item  # A retry never reactivates a historical deployment.
        if _active_id(conn) != item["expected_active_id"]:
            raise ValueError("Otro despliegue cambió el activo; no se confirma un intento rezagado")
        number = conn.execute("SELECT COALESCE(MAX(number),0)+1 FROM relay_deployment_attempts").fetchone()[0]
        conn.execute("UPDATE relay_deployment_attempts SET status='confirmed',number=?,deployed_at=?,runtime_pid=? "
                     "WHERE deployment_id=?", (number, now(), runtime_pid, deployment_id))
        conn.execute("INSERT INTO relay_deployment_active(singleton,deployment_id) VALUES(1,?) "
                     "ON CONFLICT(singleton) DO UPDATE SET deployment_id=excluded.deployment_id", (deployment_id,))
        return _attempt(conn, deployment_id)
    return _write(path, operation)


def fail(path, deployment_id):
    def operation(conn):
        item = _attempt(conn, deployment_id)
        if not item or item["status"] == "confirmed":
            raise ValueError("Sólo se puede marcar fallido un intento no confirmado")
        conn.execute("UPDATE relay_deployment_attempts SET status='failed' WHERE deployment_id=?", (deployment_id,))
        return _attempt(conn, deployment_id)
    return _write(path, operation)


def capture_runtime(repo_root=None):
    """Bind this process once to loaded source and an explicit candidate/active receipt."""
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[3]
    commit, dirty = None, None
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=3, check=True)
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True, timeout=3, check=True)
        commit = _validate(head.stdout.strip(), _SHA, "commit_sha")
        dirty = bool(status.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    runtime = {"commit_sha": commit, "software_version": __version__, "pid": os.getpid(),
               "started_at": now(), "deployment_id": os.environ.get("RELAY_DEPLOYMENT_ID") or None,
               "dirty": dirty, "_db_path": str(default_db_path())}
    if not runtime["deployment_id"] and not dirty and commit:
        try:
            active = history(runtime["_db_path"])["active"]
            if (active and (active["commit_sha"], active["software_version"]) == (commit, __version__)
                    and active.get("runtime_pid") == runtime["pid"]):
                runtime["deployment_id"] = active["deployment_id"]
        except sqlite3.Error:
            pass
    return runtime


def _health_payload(runtime):
    if not runtime:
        return {"software_version": __version__, "runtime": None, "deployment": None, "deployment_state": "unknown"}
    result = {"software_version": runtime["software_version"],
              "runtime": {key: value for key, value in runtime.items() if not key.startswith("_")},
              "deployment": None, "deployment_state": "unknown"}
    try:
        recorded = history(runtime["_db_path"])
    except sqlite3.Error:
        return result
    if not runtime["commit_sha"] or runtime["dirty"] is not False:
        return result
    active, bound_id = recorded["active"], runtime["deployment_id"]
    matches = lambda item: item and (item["deployment_id"], item["commit_sha"], item["software_version"]) == (
        bound_id, runtime["commit_sha"], runtime["software_version"])
    if matches(active) and active.get("runtime_pid") == runtime.get("pid"):
        result.update(deployment=active, deployment_state="active")
    else:
        bound = next((item for item in recorded["attempts"] if item["deployment_id"] == bound_id), None)
        result["deployment_state"] = "pending" if matches(bound) and bound["status"] == "pending" else "unconfirmed"
    return result


async def health_payload(app):
    return await asyncio.to_thread(_health_payload, app.get(RUNTIME_KEY))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db_path())
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("prepare")
    begin.add_argument("--deployment-id", required=True)
    begin.add_argument("--commit-sha", required=True)
    begin.add_argument("--software-version", required=True)
    begin.add_argument("--rollback-of")
    begin.add_argument("--target-deployment-id")
    end = commands.add_parser("confirm")
    end.add_argument("--deployment-id", required=True)
    end.add_argument("--runtime-commit-sha", required=True)
    end.add_argument("--runtime-software-version", required=True)
    end.add_argument("--runtime-pid", type=int, required=True)
    end.add_argument("--runtime-deployment-id", required=True)
    failed = commands.add_parser("fail")
    failed.add_argument("--deployment-id", required=True)
    commands.add_parser("history")
    args = vars(parser.parse_args(argv))
    command, path = args.pop("command"), args.pop("db")
    try:
        result = globals()[command](path, **args)
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
