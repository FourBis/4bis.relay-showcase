"""Contexto de logging por run: el chat_id en cada línea, sin tocar call sites.

El problema que lo motivó (2026-07-25): el relay logueaba bien pero no
había forma de preguntar "muéstrame todo lo que pasó en el chat X".
Diagnosticar un run muerto obligaba a reconstruirlo a mano desde la fila
de `chats`, el `messages_json` de la conversación y mirando procesos
vivos del sistema. Eso fue el grueso del tiempo de la investigación del
un chat de ejemplo.

Un `ContextVar` seteado una vez al arrancar el run alcanza: los ~200
`logger.warning(...)` que ya están desperdigados por el codebase ganan el
chat_id gratis, y los contextvars se propagan solos a las tasks de
asyncio que el run crea adentro (watchdog de idle, callbacks de
progreso), que es justo donde pasan las cosas interesantes.

Módulo propio y no un rincón de config.py porque no es configuración, y
porque tiene que poder importarlo cualquiera (server, admin, night) sin
armar un ciclo. Es hoja: solo depende de la stdlib.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar

current_chat: ContextVar[str] = ContextVar("relay_chat_id", default="")
current_project: ContextVar[str] = ContextVar("relay_project", default="")


class ChatContextFilter(logging.Filter):
    """Inyecta `chat_id` y `project` en cada LogRecord. Nunca descarta.

    OJO dónde se instala: tiene que ir en los HANDLERS, no en el logger
    root. Un filter a nivel logger solo ve los records emitidos DIRECTO
    contra ese logger — los que propagan desde los hijos (`relay.experts`,
    `relay.mcp_pool`, ...) no pasan por él, y esos son todos los que nos
    importan. En los handlers sí los ve, porque el record ya subió.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.chat_id = current_chat.get()
        record.project = current_project.get()
        return True


def bind(chat_id: str = "", project: str = "") -> None:
    """Asocia a un chat todo lo que se loguee de acá en más.

    Llamar al principio del run, ya adentro de la task que lo corre: el
    ContextVar se setea en el contexto de esa task y lo heredan las que
    cree, no las de al lado. Dos runs en paralelo no se pisan.
    """
    current_chat.set(chat_id or "")
    current_project.set(project or "")
