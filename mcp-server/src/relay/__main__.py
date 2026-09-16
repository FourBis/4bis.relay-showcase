"""CLI del relay — `python -m relay night start|stop|status|report`.

ADR-028: UI y CLI comparten los MISMOS endpoints HTTP del relay
(el CLI no importa el orquestador — habla con el proceso que ya
corre en :8413). Ponytail: argparse + httpx sync, sin framework CLI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

BASE = os.environ.get("RELAY_URL", "http://127.0.0.1:8413")


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    try:
        r = httpx.request(method, BASE + path, json=payload, timeout=10.0)
    except httpx.HTTPError as e:
        print(f"error: no pude hablar con el relay en {BASE} ({e})",
              file=sys.stderr)
        sys.exit(2)
    try:
        data = r.json()
    except json.JSONDecodeError:
        data = {"error": r.text[:200]}
    if r.status_code >= 400:
        print(f"error {r.status_code}: {data.get('error', data)}",
              file=sys.stderr)
        sys.exit(1)
    return data


def night_start(args: argparse.Namespace) -> None:
    payload: dict = {"project": args.project}
    if args.deadline:
        payload["deadline_iso"] = args.deadline
    if args.directive:
        payload["directive"] = args.directive
    if args.error_logs:
        payload["error_logs"] = Path(args.error_logs).read_text(
            encoding="utf-8", errors="replace")
    data = _call("POST", "/night-mode/start", payload)
    print(json.dumps(data, indent=2, ensure_ascii=False))


def night_stop(args: argparse.Namespace) -> None:
    data = _call("POST", "/night-mode/stop", {"run_id": args.run})
    print(json.dumps(data, indent=2, ensure_ascii=False))


def night_status(args: argparse.Namespace) -> None:
    data = _call("GET", f"/night-mode/status?run_id={args.run}")
    print(json.dumps(data, indent=2, ensure_ascii=False))


def night_report(args: argparse.Namespace) -> None:
    data = _call("GET", f"/night-mode/status?run_id={args.run}")
    path = data.get("report_path")
    if not path:
        print("el run no tiene reporte todavía (¿sigue corriendo?). "
              f"status={data.get('status', data.get('end_reason'))}",
              file=sys.stderr)
        sys.exit(1)
    print(Path(path).read_text(encoding="utf-8", errors="replace"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="relay")
    sub = parser.add_subparsers(dest="cmd", required=True)

    night = sub.add_parser("night", help="modo nocturno (ADR-028)")
    nsub = night.add_subparsers(dest="night_cmd", required=True)

    p = nsub.add_parser("start", help="arranca un night run")
    p.add_argument("project", help="slug del proyecto")
    p.add_argument("--deadline", default="", help="ISO 8601 (default 7am)")
    p.add_argument("--directive", default="", help="directiva humana (Fase 1)")
    p.add_argument("--error-logs", dest="error_logs", default="",
                   help="path a un archivo de logs de error (Fase 1)")
    p.set_defaults(func=night_start)

    p = nsub.add_parser("stop", help="para un run después de la tarea actual")
    p.add_argument("--run", required=True, help="run_id")
    p.set_defaults(func=night_stop)

    p = nsub.add_parser("status", help="snapshot del run")
    p.add_argument("--run", required=True, help="run_id")
    p.set_defaults(func=night_status)

    p = nsub.add_parser("report", help="imprime el reporte de la mañana")
    p.add_argument("--run", required=True, help="run_id")
    p.set_defaults(func=night_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
