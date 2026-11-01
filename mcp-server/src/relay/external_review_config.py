"""Owner-only project configuration; never changes accounts or human risk policy."""
from __future__ import annotations

import re


def validate(value, *, model_specs, users):
    allowed = {"enabled", "model", "publication_mode", "publisher_actor", "recipients"}
    if not isinstance(value, dict) or set(value) - allowed or type(value.get("enabled")) is not bool:
        raise ValueError("La revisión requiere enabled booleano y sólo campos de configuración admitidos")
    model = value.get("model", "")
    if not isinstance(model, str) or len(model) > 200 or any(ord(c) < 32 for c in model):
        raise ValueError("Selecciona un modelo existente del catálogo")
    model = model.strip()
    if model and model not in model_specs:
        raise ValueError("El modelo no pertenece al catálogo de Relay")
    if value["enabled"] and not model:
        raise ValueError("Selecciona un modelo explícito antes de habilitar revisión automática")
    mode = value.get("publication_mode", "")
    if mode not in {"", "personal", "bot"}:
        raise ValueError("publication_mode debe ser personal o bot, o vacío para no publicar")
    actor = value.get("publisher_actor", "")
    if not isinstance(actor, str) or len(actor) > 254:
        raise ValueError("Identidad de publicación inválida")
    actor = actor.strip().lower()
    active = {u["email"].lower() for u in users if u.get("enabled", True)}
    if actor and actor not in active:
        raise ValueError("La identidad de publicación no es una cuenta Relay activa")
    recipients = value.get("recipients", [])
    if not isinstance(recipients, list) or len(recipients) > 50:
        raise ValueError("recipients requiere una lista explícita de cuentas Relay")
    emails = []
    for email in recipients:
        if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
            raise ValueError("Cada destinatario debe ser un correo de cuenta Relay")
        email = email.strip().lower()
        if email not in active:
            raise ValueError("Un destinatario no pertenece a las cuentas activas de Relay")
        if email not in emails:
            emails.append(email)
    return {"enabled": value["enabled"], "model": model, "publication_mode": mode,
            "publisher_actor": actor, "recipients": emails}
