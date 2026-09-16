"""Persistencia de prompts en JSONL, un archivo por prompt.

Iter 1 (ADR-009): un prompt lleva target + system + user. Después de
que la extensión responde vía POST /prompts/{id}/response, marcamos el
prompt como `responded` (o `errored`) y disparamos /notify al bot C#.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _path_for(prompts_dir: Path, prompt_id: str) -> Path:
    safe = "".join(c for c in prompt_id if c.isalnum() or c in "_-")
    if safe != prompt_id or not safe:
        raise ValueError(f"prompt_id inválido: {prompt_id!r}")
    return prompts_dir / f"{safe}.jsonl"


def _new_prompt_id() -> str:
    return "prm_" + secrets.token_hex(3)


@dataclass
class PromptStore:
    prompts_dir: Path

    def __post_init__(self) -> None:
        self.prompts_dir.mkdir(parents=True, exist_ok=True)

    async def create(
        self,
        target: str,
        source: str,
        author: str,
        user: str,
        system: str = "",
        prompt_id: Optional[str] = None,
    ) -> str:
        """Crea un prompt nuevo. Devuelve su id.

        Si te pasan prompt_id se respeta (útil para tests / reintentos
        idempotentes). El payload NO incluye `system` cuando viene vacío.
        """
        pid = prompt_id or _new_prompt_id()
        await self.append_event(pid, "created", {
            "target": target,
            "source": source,
            "author": author,
            "user": user,
            "system": system,
        })
        return pid

    async def append_event(self, prompt_id: str, event: str, data: dict[str, Any]) -> None:
        path = _path_for(self.prompts_dir, prompt_id)
        line = json.dumps(
            {"ts": now_iso(), "event": event, "data": data},
            ensure_ascii=False, separators=(",", ":"),
        )
        def _write() -> None:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        await asyncio.to_thread(_write)

    async def read(self, prompt_id: str) -> dict[str, Any] | None:
        path = _path_for(self.prompts_dir, prompt_id)
        if not path.exists():
            return None

        def _read() -> dict[str, Any] | None:
            with path.open("r", encoding="utf-8") as f:
                lines = [l.rstrip("\n") for l in f if l.strip()]
            if not lines:
                return None
            try:
                created = json.loads(lines[0])
            except json.JSONDecodeError:
                return None
            d = created.get("data", {})

            status = "pending"
            response = None
            for line in lines[1:]:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") == "responded":
                    status = "responded"
                    response = rec.get("data")
                elif rec.get("event") == "errored":
                    status = "errored"
                    response = rec.get("data")

            return {
                "id": prompt_id,
                "ts": created.get("ts"),
                "target": d.get("target"),
                "source": d.get("source"),
                "author": d.get("author"),
                "user": d.get("user"),
                "system": d.get("system"),
                "status": status,
                "response": response,
            }
        return await asyncio.to_thread(_read)

    async def mark_responded(self, prompt_id: str, content: str) -> bool:
        if not (await self.exists(prompt_id)):
            return False
        await self.append_event(prompt_id, "responded", {"content": content})
        return True

    async def mark_errored(self, prompt_id: str, error: str) -> bool:
        if not (await self.exists(prompt_id)):
            return False
        await self.append_event(prompt_id, "errored", {"error": error})
        return True

    async def exists(self, prompt_id: str) -> bool:
        return _path_for(self.prompts_dir, prompt_id).exists()
