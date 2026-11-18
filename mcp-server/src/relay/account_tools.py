"""Tools del chat con la cuenta del autor del turno, nunca del proceso."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx
from pydantic_ai import Tool

from . import github
from .pr_text import neutralize_issue_closing_keywords
from .user_accounts import AccountError, current_actor, require_account


async def github_request(method: str, repo_path: str, suffix: str, *, payload=None):
    account = await require_account("github")
    repo = await github.repo_slug(repo_path)
    if not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise AccountError("El proyecto no tiene un repositorio GitHub válido.")
    if method == "POST" and suffix == "pulls" and isinstance(payload, dict):
        payload = {**payload, "body": neutralize_issue_closing_keywords(payload.get("body") or "")}
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


def meeting_guidance(now: datetime | None = None) -> str:
    """Contexto para agendar: la fecha real evita resolver 'el próximo lunes' a ciegas."""
    now = now or datetime.now(timezone.utc)
    return ("Calendar (cuenta del turno): calendar_events y calendar_free_slots leen; meeting_prepare "
            "y calendar_prepare_event solo preparan, y el titular confirma en Mi cuenta. "
            "Ante una solicitud de reunión por correo: abre el correo con gmail_message, busca huecos "
            "con calendar_free_slots y prepara respuesta + evento con meeting_prepare. "
            "Los eventos que leas también quedan en el hilo compartido. "
            f"Fecha y hora actual (UTC): {now.strftime('%A %Y-%m-%d %H:%M')}. Si la fecha pedida ya "
            "pasó o el día de semana no coincide con la fecha, no la asumas: indícalo y propone "
            "alternativas o pregunta al titular.")


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
                            bcc: list[str] | None = None, reply_to_message_id: str | None = None) -> str:
        """Prepara un correo propio. No lo envía: el titular lo revisa y pulsa Enviar en Mi cuenta.

        Con reply_to_message_id (id de gmail_read) el correo sale como respuesta en el mismo hilo.
        """
        from .tools.gmail import GmailSendTool
        args = {"to": to, "subject": subject, "body": body, "cc": cc or [], "bcc": bcc or []}
        if reply_to_message_id:
            args["reply_to"] = reply_to_message_id
        return json.dumps(await GmailSendTool().call(args), ensure_ascii=False)

    async def calendar_events(time_min: str, time_max: str, max_results: int = 25) -> str:
        """Lista eventos del Google Calendar propio en un rango ISO 8601 con zona (máx. 31 días)."""
        from .user_calendar import mcp_calendar_events
        return json.dumps(await mcp_calendar_events(
            {"time_min": time_min, "time_max": time_max, "max_results": max_results}), ensure_ascii=False)

    async def calendar_free_slots(time_min: str, time_max: str, duration_minutes: int = 60,
                                  day_start: str = "09:00", day_end: str = "19:00",
                                  include_weekends: bool = False) -> str:
        """Calcula huecos libres del calendario propio dentro del horario indicado (HH:MM, zona del calendario).

        Usa esto, no aritmética propia, para proponer o confirmar horarios de reunión.
        """
        from .user_calendar import mcp_calendar_free_slots
        return json.dumps(await mcp_calendar_free_slots({
            "time_min": time_min, "time_max": time_max, "duration_minutes": duration_minutes,
            "day_start": day_start, "day_end": day_end, "include_weekends": include_weekends}),
            ensure_ascii=False)

    async def meeting_prepare(reply_to_message_id: str, to: list[str], subject: str, body: str,
                              summary: str, start: str, end: str, attendees: list[str] | None = None,
                              location: str = "", description: str = "", meet: bool | None = None,
                              cc: list[str] | None = None) -> str:
        """Prepara la respuesta a una solicitud de reunión y el evento con invitación, en un solo borrador.

        No envía ni agenda: el titular revisa en Mi cuenta y con un clic envía la respuesta en el hilo
        y crea el evento. start/end en ISO 8601 con zona. meet=None agrega Google Meet salvo que haya
        location (reunión presencial). attendees por defecto = to + cc.
        """
        from .user_mail import mcp_create_mail_draft
        invitees = list(attendees) if attendees else [*to, *(cc or [])]
        event = {"summary": summary, "start": start, "end": end, "attendees": invitees,
                 "location": location, "description": description}
        if meet is not None:
            event["meet"] = meet
        return json.dumps(await mcp_create_mail_draft({
            "to": to, "cc": cc or [], "subject": subject, "body": body,
            "reply_to": reply_to_message_id, "event": event}), ensure_ascii=False)

    async def calendar_prepare_event(summary: str, start: str, end: str, attendees: list[str] | None = None,
                                     location: str = "", description: str = "",
                                     meet: bool | None = None) -> str:
        """Prepara un evento sin correo asociado. El titular lo crea desde Mi cuenta (envía invitaciones)."""
        from .user_calendar import mcp_prepare_event
        event = {"summary": summary, "start": start, "end": end, "attendees": attendees or [],
                 "location": location, "description": description}
        if meet is not None:
            event["meet"] = meet
        return json.dumps(await mcp_prepare_event(event), ensure_ascii=False)

    async def gmail_message(message_id: str) -> str:
        """Lee el cuerpo de un correo propio por su ID; el resultado queda en el hilo compartido."""
        from .tools.gmail import GmailReadTool
        return json.dumps(await GmailReadTool().call({"message_id": message_id}), ensure_ascii=False)

    # A project consultation does not inherit unrelated personal mail access.
    private_consultation = bool((project.get("defaults_json") or {}).get("mcp_private"))
    functions = [] if private_consultation else [
        gmail_read, gmail_message, gmail_prepare,
        calendar_events, calendar_free_slots, meeting_prepare, calendar_prepare_event]
    if repo_path:
        functions += [github_issues, github_issue]
        if not read_only and not private_consultation:
            functions += [github_create_issue, github_comment, github_create_pr]
    return [Tool(fn, takes_ctx=False) for fn in functions]
