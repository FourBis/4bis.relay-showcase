"""Tools del chat con la cuenta del autor del turno, nunca del proceso."""
from __future__ import annotations

import json
import re

import httpx
from pydantic_ai import Tool

from . import github
from .user_accounts import AccountError, current_actor, require_account


async def github_request(method: str, repo_path: str, suffix: str, *, payload=None):
    account = await require_account("github")
    repo = await github.repo_slug(repo_path)
    if not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise AccountError("El proyecto no tiene un repositorio GitHub válido.")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.request(method, f"https://api.github.com/repos/{repo}/{suffix}",
                headers={"Authorization": f"Bearer {account['access_token']}",
                         "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                json=payload)
        if response.status_code == 401:
            store, email = current_actor.get()
            await store.invalidate("github", email)
        if not 200 <= response.status_code < 300:
            raise AccountError(f"GitHub rechazó la operación (HTTP {response.status_code}); revisa tu conexión y acceso al repositorio.")
        return {"actor": account["login"], "result": response.json()}
    except (httpx.HTTPError, ValueError):
        raise AccountError("No se pudo confirmar el resultado en GitHub. Revisa el repositorio antes de repetir la acción.") from None


def account_tools(project: dict, *, read_only=False):
    actor = current_actor.get()
    if actor is None or not actor[1] or actor[1] == "owner":
        return []
    if (project.get("defaults_json") or {}).get("task_feedback"):
        return []  # Feedback externo no puede iniciar operaciones de cuentas personales.
    repo_path = project.get("repo_path") or ""

    async def invoke(method, suffix, payload=None):
        try:
            return json.dumps(await github_request(method, repo_path, suffix, payload=payload), ensure_ascii=False)
        except AccountError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    async def github_issues() -> str:
        """Lee los issues y PRs abiertos del proyecto con tu acceso GitHub."""
        return await invoke("GET", "issues?state=open&per_page=30")

    async def github_issue(number: int) -> str:
        """Lee un issue/PR y sus comentarios de conversación en el proyecto."""
        if number < 1:
            return "El número debe ser positivo."
        item = await invoke("GET", f"issues/{number}")
        comments = await invoke("GET", f"issues/{number}/comments?per_page=50")
        return f"{item}\nComentarios: {comments}"

    async def github_create_issue(title: str, body: str) -> str:
        """Crea un issue en el proyecto como el usuario del turno, si lo pidió."""
        if not title.strip() or len(title) > 256 or len(body) > 60000:
            return "Título o cuerpo inválido."
        return await invoke("POST", "issues", {"title": title, "body": body})

    async def github_comment(number: int, body: str) -> str:
        """Publica una respuesta en un issue o PR como el usuario del turno."""
        if number < 1 or not body.strip() or len(body) > 60000:
            return "Número o comentario inválido."
        return await invoke("POST", f"issues/{number}/comments", {"body": body})

    async def github_create_pr(title: str, body: str, head: str, base: str = "develop") -> str:
        """Abre una PR borrador del proyecto con una rama ya subida y la cuenta del turno."""
        if (not title.strip() or len(title) > 256 or len(body) > 60000
                or not head or not base or len(head) > 200 or len(base) > 200):
            return "Título, cuerpo o ramas inválidas."
        return await invoke("POST", "pulls", {"title": title, "body": body,
                            "head": head, "base": base, "draft": True})

    async def gmail_read(query: str = "", max_results: int = 10) -> str:
        """Lee correos de la cuenta propia. El resultado se comparte con este hilo de trabajo."""
        from .tools.gmail import GmailReadTool
        return json.dumps(await GmailReadTool().call({"query": query, "max_results": max_results}), ensure_ascii=False)

    async def gmail_prepare(to: str, subject: str, body: str, cc: list[str] | None = None,
                            bcc: list[str] | None = None) -> str:
        """Prepara un correo propio. No lo envía: el titular lo revisa y pulsa Enviar en Mi cuenta."""
        from .tools.gmail import GmailSendTool
        return json.dumps(await GmailSendTool().call(
            {"to": to, "subject": subject, "body": body, "cc": cc or [], "bcc": bcc or []}), ensure_ascii=False)

    async def gmail_message(message_id: str) -> str:
        """Lee el cuerpo de un correo propio por su ID; el resultado queda en el hilo compartido."""
        from .tools.gmail import GmailReadTool
        return json.dumps(await GmailReadTool().call({"message_id": message_id}), ensure_ascii=False)

    functions = [gmail_read, gmail_message, gmail_prepare]
    if repo_path:
        functions += [github_issues, github_issue]
        if not read_only:
            functions += [github_create_issue, github_comment, github_create_pr]
    return [Tool(fn, takes_ctx=False) for fn in functions]
