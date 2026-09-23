"""Preguntas interactivas de expertos y del modo nocturno."""
from __future__ import annotations

import json
from typing import Any, Optional

from .db_support import now_iso

class DatabaseQuestionsMixin:

    async def create_expert_question(
        self, q_id: str, chat_id: str, question_json: str, *,
        conversation_id: Optional[str] = None,
        project_slug: Optional[str] = None, kind: str = "choice",
    ) -> int:
        """Anota una pregunta y JUBILA las que quedaron abiertas antes.

        Devuelve cuántas jubiló (2026-08-16).

        Por qué jubilar: una pregunta abierta de un turno anterior es una
        decisión que el hilo ya dejó atrás. Si el humano no la contestó y
        el experto volvió a preguntar, la vieja no solo es ruido —
        contestarla inyecta como turno siguiente una decisión sobre algo
        que ya no está en juego. Se veía como "la decisión de la UI se
        repite": cada turno agregaba una pregunta más a la pila y nadie
        sacaba las de antes.

        `superseded` y no `skipped`: skipped es "el humano la descartó",
        y esto no lo decidió el humano. Además `answer_expert_question`
        exige `status='open'`, así que una jubilada ya no se puede
        contestar de rebote desde una pestaña vieja.
        """
        # Por conversación si hay hilo; si no, por run. Sin esto, dos
        # conversaciones distintas del mismo proyecto se jubilarían entre
        # ellas.
        # …salvo las de un NODO DE GRAFO que sigue esperando (2026-08-31).
        #
        # Jubilar por conversación asume un solo hilo de decisión, que es
        # verdad en el chat: cada turno reemplaza al anterior. En un
        # grafo NO: varios nodos corren en paralelo sobre la MISMA
        # conversación y cada uno pregunta por su propia tarea, así que
        # la pregunta del nodo B mataba la del nodo A. Y como nada saca
        # a esa tarea de `esperando_humano` —y `answer_expert_question`
        # exige `status='open'`— el nodo quedaba inalcanzable: ni
        # contestándolo desde la UI ni con `/graphs/{id}/resume`.
        # Medido: 10 tareas trabadas en 3 grafos, algunas desde hacía 6
        # horas, arrastrando a sus dependientes.
        #
        # Protege a CUALQUIER chat que sea un nodo de grafo, sin mirar el
        # estado de la tarea. La primera versión de esto exigía
        # `estado='esperando_humano'` y llegaba tarde: la tarea recién
        # pasa a ese estado cuando el run TERMINA (`_cerrar_tarea`),
        # mientras que la pregunta nace a mitad del run. En esa ventana
        # el nodo figura `corriendo` y quedaba desprotegido. Se vio en
        # vivo el 1/9: el nodo de EmailSender preguntó 03:28:30 y el de
        # i18n le jubiló la pregunta 03:28:39, nueve segundos después.
        #
        # La tarea que RE-pregunta no se auto-protege: al re-ejecutarse
        # el run nuevo le pisa el `chat_id` a la fila de la tarea, así
        # que el chat viejo deja de pertenecer a ningún nodo y su
        # pregunta vuelve a ser jubilable. No quedan dos abiertas para el
        # mismo nodo.
        if conversation_id:
            filas = await self.run(
                "UPDATE expert_questions SET status='superseded', "
                "answered_at=? WHERE conversation_id=? AND status='open' "
                "AND chat_id NOT IN ("
                "  SELECT chat_id FROM tasks WHERE COALESCE(chat_id,'')!=''"
                ") RETURNING id", (now_iso(), conversation_id))
        else:
            filas = await self.run(
                "UPDATE expert_questions SET status='superseded', "
                "answered_at=? WHERE chat_id=? AND status='open' "
                "RETURNING id", (now_iso(), chat_id))
        await self.run(
            "INSERT INTO expert_questions "
            "(id, chat_id, conversation_id, project_slug, kind, "
            " question_json, asked_at) VALUES (?,?,?,?,?,?,?)",
            (q_id, chat_id, conversation_id, project_slug, kind,
             question_json, now_iso()))
        return len(filas)

    async def get_expert_question(self, q_id: str) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM expert_questions WHERE id=?", (q_id,))
        return rows[0] if rows else None

    async def list_expert_questions(
        self, *, conversation_id: Optional[str] = None,
        chat_id: Optional[str] = None, only_open: bool = True,
        limit: int = 20,
    ) -> list[dict]:
        where, params = [], []
        if conversation_id:
            where.append("conversation_id=?"); params.append(conversation_id)
        if chat_id:
            where.append("chat_id=?"); params.append(chat_id)
        if only_open:
            where.append("status='open'")
        sql = "SELECT * FROM expert_questions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY asked_at DESC LIMIT {int(limit)}"
        return await self.run(sql, tuple(params))

    async def answer_expert_question(
        self, q_id: str, answer_json: str,
    ) -> bool:
        """Responde una pregunta ABIERTA. False si ya estaba cerrada.

        El `status='open'` en el WHERE es lo que hace idempotente al
        doble click del humano (y a Discord + Admin UI respondiendo la
        misma pregunta): la segunda respuesta no pisa a la primera.
        """
        rows = await self.run(
            "UPDATE expert_questions SET answer_json=?, answered_at=?, "
            "status='answered' WHERE id=? AND status='open' RETURNING id",
            (answer_json, now_iso(), q_id))
        return bool(rows)

    async def skip_expert_question(self, q_id: str) -> None:
        await self.run(
            "UPDATE expert_questions SET status='skipped', answered_at=? "
            "WHERE id=? AND status='open'", (now_iso(), q_id))

    async def create_night_question(
        self, q_id: str, run_id: str, phase: str,
        question_json: str, notify_target: Optional[str] = None,
        notify_via: str = "both",
    ) -> None:
        await self.run(
            "INSERT INTO night_questions "
            "(id, run_id, phase, question_json, asked_at, "
            " notified_via, notify_target) VALUES (?,?,?,?,?,?,?)",
            (q_id, run_id, phase, question_json, now_iso(),
             notify_via, notify_target))

    async def get_night_question(self, q_id: str) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM night_questions WHERE id=?", (q_id,))
        return rows[0] if rows else None

    async def get_night_question_answer(
        self, q_id: str,
    ) -> Optional[dict]:
        """Retorna el answer_json parseado si la pregunta está respondida,
        None si todavía no. Usado por ask_and_wait() en su poll loop."""
        rows = await self.run(
            "SELECT answer_json, answered_at FROM night_questions "
            "WHERE id=? AND answered_at IS NOT NULL",
            (q_id,))
        if not rows:
            return None
        answer_json = rows[0]["answer_json"]
        try:
            return json.loads(answer_json) if answer_json else {}
        except json.JSONDecodeError:
            return {}

    async def answer_night_question(
        self, q_id: str, answer: dict,
    ) -> bool:
        """Guarda la respuesta del humano. Idempotente: si ya estaba
        respondida, retorna False (la API HTTP responde 409 en ese caso).

        Bug fix 2026-07-18: era SELECT-luego-UPDATE sin transacción —
        dos respuestas concurrentes podían leer ambas `answered_at IS
        NULL` y recibir ambas True. La cláusula `WHERE answered_at IS
        NULL` del UPDATE impedía la doble escritura, pero el caller HTTP
        quedaba mintiendo sobre quién ganó. Ahora es atómico:
        `UPDATE ... RETURNING answered_at` y solo retornamos True si
        el RETURNING trajo una fila (osea, la nuestra ganó el race).
        """
        rows = await self.run(
            "UPDATE night_questions SET answered_at=?, answer_json=? "
            "WHERE id=? AND answered_at IS NULL "
            "RETURNING id",
            (now_iso(), json.dumps(answer), q_id))
        return bool(rows)

    async def skip_night_question(self, q_id: str) -> bool:
        """Cierra la pregunta sin respuesta (default). Misma semántica
        que answer_night_question pero con answer={}."""
        return await self.answer_night_question(q_id, {})

    async def list_night_questions(
        self, run_id: Optional[str] = None,
        only_open: bool = False,
    ) -> list[dict]:
        where_parts = []
        params: list[Any] = []
        if run_id:
            where_parts.append("run_id=?")
            params.append(run_id)
        if only_open:
            where_parts.append("answered_at IS NULL")
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        return await self.run(
            f"SELECT * FROM night_questions {where} "
            "ORDER BY asked_at DESC", tuple(params))

    async def orphan_open_questions(self) -> int:
        """Marca como skipped todas las preguntas abiertas de runs que
        no están activos en memoria. Llamado al startup del relay
        para limpiar preguntas huérfanas de runs que murieron con el
        proceso. Retorna cuántas cerró."""
        # Heurística: runs con ended_at != NULL cuyas preguntas siguen
        # abiertas. Si el run está vivo (ended_at IS NULL), no toca.
        rows = await self.run(
            "SELECT q.id FROM night_questions q "
            "JOIN night_runs r ON r.id = q.run_id "
            "WHERE q.answered_at IS NULL AND r.ended_at IS NOT NULL")
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        # Answer con {} = "skip". Mantiene el rastro en el morning report.
        for q_id in ids:
            await self.answer_night_question(q_id, {"skipped": True})
        return len(ids)
