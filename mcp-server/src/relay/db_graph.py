"""Persistencia de grafos de tareas y claims de archivos."""
from __future__ import annotations

import json
import asyncio
from . import config
from typing import Optional

from .db_support import _vence_en, now_iso

class DatabaseGraphMixin:

    async def create_task_graph(
        self, graph_id: str, objetivo: str, *, tareas: list,
        conversation_id: Optional[str] = None,
        project_slug: Optional[str] = None,
    ) -> dict:
        """Guarda un grafo entero. `tareas` son dicts con `id` y `deps`.

        Valida ANTES de escribir (ciclos, deps colgadas, ids repetidos):
        el grafo lo escribe un LLM y un grafo inválido a medio guardar es
        peor que uno rechazado — el primero se descubre tres nodos
        después, con trabajo ya hecho encima.
        """
        from . import grafo as grafo_mod

        nodos = [grafo_mod.Nodo(
            id=t["id"], titulo=t.get("titulo") or "",
            deps=tuple(t.get("deps") or ()),
            idempotente=bool(t.get("idempotente")),
            detalle=t.get("detalle") or "", orden=int(t.get("orden") or i),
            archivos=tuple(t.get("archivos") or ()),
            max_intentos=int(t.get("max_intentos") or 2))
            for i, t in enumerate(tareas)]
        grafo_mod.validar(nodos)

        ahora = now_iso()
        # TODO en una transacción. Antes eran llamadas sueltas y el
        # INSERT del grafo commiteaba solo: si fallaba una tarea —el 24/8
        # fue un choque de `tasks.id`, que es PK global— quedaba un grafo
        # `activo` con cero tareas, que además traba el hilo porque
        # `active_task_graph` lo devuelve y no deja armar otro.
        sentencias: list = [(
            "INSERT INTO task_graphs (id, conversation_id, project_slug, "
            "objetivo, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (graph_id, conversation_id, project_slug, objetivo, ahora, ahora))]
        for n in nodos:
            sentencias.append((
                "INSERT INTO tasks (id, graph_id, titulo, detalle, "
                "idempotente, max_intentos, orden, archivos) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (n.id, graph_id, n.titulo, n.detalle,
                 1 if n.idempotente else 0, n.max_intentos, n.orden,
                 json.dumps(list(n.archivos)))))
            for d in n.deps:
                sentencias.append((
                    "INSERT INTO task_deps (graph_id, task_id, depende_de) "
                    "VALUES (?,?,?)", (graph_id, n.id, d)))
        await self.run_tx(sentencias)
        return await self.get_task_graph(graph_id)

    async def get_task_graph(self, graph_id: str) -> Optional[dict]:
        filas = await self.run(
            "SELECT * FROM task_graphs WHERE id=?", (graph_id,))
        if not filas:
            return None
        g = dict(filas[0])
        g["tasks"] = await self.list_tasks(graph_id)
        return g

    async def list_tasks(self, graph_id: str) -> list[dict]:
        """Las tareas con sus `deps` resueltas, en orden estable."""
        tareas = await self.run(
            "SELECT * FROM tasks WHERE graph_id=? ORDER BY orden, id",
            (graph_id,))
        aristas = await self.run(
            "SELECT task_id, depende_de FROM task_deps WHERE graph_id=?",
            (graph_id,))
        por_tarea: dict = {}
        for a in aristas:
            por_tarea.setdefault(a["task_id"], []).append(a["depende_de"])
        for t in tareas:
            t["deps"] = sorted(por_tarea.get(t["id"], []))
        return tareas

    async def add_tasks_to_graph(
        self, graph_id: str, tareas: list, *,
        reemplaza: Optional[str] = None,
    ) -> list[str]:
        """Agrega tareas a un grafo QUE YA ESTÁ CORRIENDO (Fase 2A, 2026-09-02).

        `orquestador._vueltas` relee el grafo entero en cada iteración
        (`get_task_graph`), así que las tareas agregadas acá se levantan
        solas en la vuelta siguiente — no hace falta tocar el scheduler.

        `tareas` son dicts con `id` y `deps`, mismo formato que
        `create_task_graph`: quien llama es responsable de que los ids
        ya vengan en su forma final y única (mismo criterio de
        `planificador._ids_unicos` — `tasks.id` es PK GLOBAL, no por
        grafo; acá solo se VERIFICA la colisión, no se resuelve).

        `reemplaza`: el id de una tarea EXISTENTE que se subdivide. Dos
        cosas pasan para que el grafo no quede roto en silencio:
          - los DEPENDIENTES de `reemplaza` (filas de `task_deps` con
            `depende_de=reemplaza`) se re-apuntan a TODAS las tareas
            nuevas — si no, quedarían esperando para siempre a un nodo
            que nunca va a estar `hecho`.
          - las tareas nuevas heredan las deps que tenía `reemplaza`,
            para no arrancar antes de que esas dependencias originales
            cierren.
        `reemplaza` en sí NO se toca (ni se borra ni cambia de estado):
        qué pasa con el nodo padre es otra decisión, no de acá.

        Todo o nada (`run_tx`): si una tarea no se puede insertar, no
        puede quedar el grafo con las demás a medio meter — el mismo
        bug que ya pasó con `create_task_graph` el 24/8.

        Devuelve los ids de las tareas agregadas.
        """
        from . import grafo as grafo_mod

        existentes = await self.list_tasks(graph_id)
        if not existentes:
            raise ValueError(f"grafo sin tareas o inexistente: {graph_id}")
        por_id = {t["id"]: t for t in existentes}

        nuevos_ids = [t["id"] for t in tareas]
        if not nuevos_ids:
            raise ValueError("tareas vacío: nada que agregar")
        if len(set(nuevos_ids)) != len(nuevos_ids):
            raise grafo_mod.GrafoInvalido(
                "ids repetidos entre las tareas nuevas")

        # tasks.id es PK GLOBAL (ver create_task_graph): un id que ya
        # existe en CUALQUIER grafo —este u otro— revienta el INSERT
        # con un IntegrityError feo en vez de un error legible.
        placeholders = ",".join("?" * len(nuevos_ids))
        choques = {f["id"] for f in await self.run(
            f"SELECT id FROM tasks WHERE id IN ({placeholders})",
            tuple(nuevos_ids))}
        if choques:
            raise grafo_mod.GrafoInvalido(
                f"id(s) ya existen en la base: {sorted(choques)}")

        deps_heredadas: tuple = ()
        if reemplaza is not None:
            if reemplaza not in por_id:
                raise ValueError(
                    "reemplaza apunta a una tarea que no está en el "
                    f"grafo: {reemplaza!r}")
            deps_heredadas = tuple(por_id[reemplaza]["deps"])

        base_orden = max(
            (int(t.get("orden") or 0) for t in existentes), default=0) + 1
        nodos_nuevos = []
        for i, t in enumerate(tareas):
            deps = tuple(dict.fromkeys(
                list(t.get("deps") or ()) + list(deps_heredadas)))
            orden = t.get("orden")
            nodos_nuevos.append(grafo_mod.Nodo(
                id=t["id"], titulo=t.get("titulo") or "", deps=deps,
                idempotente=bool(t.get("idempotente")),
                detalle=t.get("detalle") or "",
                orden=int(orden) if orden is not None else base_orden + i,
                archivos=tuple(t.get("archivos") or ()),
                max_intentos=int(t.get("max_intentos") or 2)))

        # Validar el grafo COMPLETO (existentes + nuevos) TAL COMO VA A
        # QUEDAR, no el de antes: si `reemplaza` tiene dependientes, acá
        # abajo se les borra la arista a `reemplaza` y se les crea una
        # por cada tarea nueva — validar contra `t["deps"]` sin ese
        # reapunte es validar un grafo distinto del que se escribe, y un
        # ciclo que el reapunte MISMO crea (dependiente → nueva → … →
        # dependiente) no se ve nunca. Se ajustan los nodos existentes
        # EN MEMORIA con el mismo reapunte antes de llamar a `validar`.
        ids_nuevos = [n.id for n in nodos_nuevos]
        nodos_existentes = []
        for t in existentes:
            deps_t = t["deps"]
            if reemplaza is not None and reemplaza in deps_t:
                deps_t = [d for d in deps_t if d != reemplaza] + ids_nuevos
            nodos_existentes.append(grafo_mod.Nodo.desde_fila(t, deps_t))
        grafo_mod.validar(nodos_existentes + nodos_nuevos)

        sentencias: list = []
        for n in nodos_nuevos:
            sentencias.append((
                "INSERT INTO tasks (id, graph_id, titulo, detalle, "
                "idempotente, max_intentos, orden, archivos, parent_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (n.id, graph_id, n.titulo, n.detalle,
                 1 if n.idempotente else 0, n.max_intentos, n.orden,
                 json.dumps(list(n.archivos)), reemplaza)))
            for d in n.deps:
                sentencias.append((
                    "INSERT INTO task_deps (graph_id, task_id, "
                    "depende_de) VALUES (?,?,?)", (graph_id, n.id, d)))

        if reemplaza is not None:
            dependientes = await self.run(
                "SELECT task_id FROM task_deps WHERE graph_id=? "
                "AND depende_de=?", (graph_id, reemplaza))
            if dependientes:
                sentencias.append((
                    "DELETE FROM task_deps WHERE graph_id=? "
                    "AND depende_de=?", (graph_id, reemplaza)))
                for dep_row in dependientes:
                    for n in nodos_nuevos:
                        sentencias.append((
                            "INSERT INTO task_deps (graph_id, task_id, "
                            "depende_de) VALUES (?,?,?)",
                            (graph_id, dep_row["task_id"], n.id)))

        await self.run_tx(sentencias)
        return [n.id for n in nodos_nuevos]

    async def hay_turnos_humanos_despues(
        self, conversation_id: str, desde: str,
    ) -> bool:
        """¿El hilo siguió trabajando después de `desde` (ISO)?

        La usa el panel para decidir si un grafo sigue siendo el estado
        del hilo o ya es su historia. Va como query y no filtrando en
        Python la lista de turnos porque el panel pregunta esto cada
        2,5s: traer 200 filas con su `stages_json` para mirar una fecha
        costaría ~800 kB por poll.

        `source<>'grafo'`: los nodos del propio plan no cuentan, o todo
        grafo se declararía superado por sí mismo al correr su primero.
        """
        if not desde:
            return False
        filas = await self.run(
            "SELECT 1 FROM chats WHERE conversation_id=? AND started_at>? "
            "AND COALESCE(source,'')<>'grafo' LIMIT 1",
            (conversation_id, desde))
        return bool(filas)

    async def active_task_graph(self, conversation_id: str) -> Optional[dict]:
        """El grafo vivo de un hilo, o None. Uno por conversación."""
        return await self._active_graph_where(
            "conversation_id", conversation_id)

    async def active_task_graph_by_project(
        self, project_slug: str,
    ) -> Optional[dict]:
        """El grafo vivo de un proyecto, o None.

        Gemelo de `active_task_graph` pero filtrando por `project_slug`:
        la guarda por conversación se saltea cuando el POST no trae
        `conversation_id`, y dos grafos sobre el mismo repo se pisan
        los archivos sin que ninguno se entere.
        """
        return await self._active_graph_where(
            "project_slug", project_slug)

    async def _active_graph_where(
        self, column: str, value: str,
    ) -> Optional[dict]:
        # Whitelist: las dos únicas columnas por las que tiene sentido
        # buscar el grafo vivo. No es defensa contra inyección — el `?`
        # parametriza el valor—, sino para que el helper no se vuelva
        # un SQL genérico accidental.
        if column not in ("conversation_id", "project_slug"):
            raise ValueError(f"columna no soportada: {column}")
        filas = await self.run(
            f"SELECT id FROM task_graphs WHERE {column}=? "
            "AND estado='activo' ORDER BY created_at DESC LIMIT 1",
            (value,))
        return await self.get_task_graph(filas[0]["id"]) if filas else None

    async def last_task_graph(self, conversation_id: str) -> Optional[dict]:
        """El grafo más reciente de un hilo, TERMINADO o no.

        Existe para que un grafo no desaparezca de la vista al cerrarse.
        `active_task_graph` filtra `estado='activo'`, así que el panel
        caía a modo lineal en cuanto la última tarea pasaba a `hecho` — y
        con él se iba lo único que decía qué se hizo, en qué orden, qué
        falló y cuántos intentos costó. El grafo terminado es el registro
        más útil que deja un pedido grande: sirve para aprender del run
        siguiente, y estaba entero en la base sin forma de leerlo.
        """
        filas = await self.run(
            "SELECT id FROM task_graphs WHERE conversation_id=? "
            "ORDER BY created_at DESC LIMIT 1", (conversation_id,))
        return await self.get_task_graph(filas[0]["id"]) if filas else None

    async def list_active_graphs(self) -> list[str]:
        """Los grafos que quedaron `activo`. Los mira el barrido del boot.

        Un grafo `activo` al arrancar el relay es, por definición, uno
        que se cortó: nadie lo está corriendo todavía en este proceso.
        """
        filas = await self.run(
            "SELECT id FROM task_graphs WHERE estado='activo' "
            "ORDER BY created_at")
        return [f["id"] for f in filas]

    async def update_task(self, task_id: str, **campos) -> Optional[dict]:
        """Actualiza una tarea. Solo columnas conocidas.

        Whitelist y no `**campos` directo al SQL: quien llama es el
        orquestador con datos que vienen de un modelo.
        """
        permitidas = {"estado", "intentos", "modelo", "chat_id", "resultado",
                      "error", "started_at", "ended_at", "idempotente",
                      "max_intentos", "titulo", "detalle", "archivos"}
        sets, params = [], []
        for k, v in campos.items():
            if k not in permitidas:
                raise ValueError(f"columna no actualizable: {k}")
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return None
        params.append(task_id)
        await self.run(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(params))
        filas = await self.run("SELECT * FROM tasks WHERE id=?", (task_id,))
        if filas:
            await self.run(
                "UPDATE task_graphs SET updated_at=? WHERE id=?",
                (now_iso(), filas[0]["graph_id"]))
        return filas[0] if filas else None

    def _claim_files_tx(self, task_id: str, graph_id: str,
                        archivos: list) -> list:
        """Cuerpo transaccional de `claim_task_files`.

        `run()` abre y commitea una conexión POR sentencia, y `run_tx()`
        recibe una lista de sentencias armada de antemano — ninguno de
        los dos sirve para "leer, decidir en Python, escribir" sin
        soltar el candado en el medio. Con eso, dos llamadas concurrentes
        leen el mismo estado ANTES de que cualquiera inserte, y las dos
        se creen dueñas: medido con la `Database` real, 9 de 40 intentos
        concurrentes (22%) dejaban dos dueños del mismo archivo.

        La solución es una sola conexión, un solo hilo, `BEGIN IMMEDIATE`
        (toma el candado de escritura YA, no al primer INSERT como el
        `BEGIN` implícito de sqlite3) para que la segunda llamada quede
        bloqueada en el `_connect()`/BEGIN hasta que la primera commitee
        o revierta — ahí sí, serializado de verdad.
        """
        from . import grafo as grafo_mod

        conn = self._connect()
        conn.isolation_level = None  # autocommit off a mano: BEGIN propio
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # El scope del choque es el proyecto del grafo, resuelto
                # ACA adentro (no lo manda el caller: el único caller de
                # producción, `orquestador.correr_grafo`, tiene el
                # `graph_id` pero no el `project`). '' si el grafo no
                # resuelve a ningún proyecto — pasa con grafos de test
                # que no lo declaran, y ahí siguen chocando entre sí
                # como antes de esta columna.
                fila = conn.execute(
                    "SELECT project_slug FROM task_graphs WHERE id=?",
                    (graph_id,)).fetchone()
                slug = (fila["project_slug"] or "") if fila else ""

                # El choque se decide con el MISMO predicado que usa el
                # planificador (`grafo._pisa`), no con un `archivo=?`:
                # una tarea que declara la carpeta `src/` choca con otra
                # que tiene tomado `src/x.py`, y un igual-a-igual no lo
                # ve. Se compara en Python y no con LIKE porque un
                # nombre de archivo puede traer `%` o `_`, que en LIKE
                # son comodines y harían coincidir de más.
                ajenas = [r["archivo"] for r in conn.execute(
                    "SELECT archivo FROM task_file_claims "
                    "WHERE task_id<>? AND project_slug=?",
                    (task_id, slug)).fetchall()]

                ahora = now_iso()
                vence = _vence_en(config.claim_ttl_s())
                rechazados = []
                for a in archivos:
                    norm = grafo_mod.norm_archivo(str(a))
                    if not norm:
                        continue
                    if any(grafo_mod._pisa(norm, otra) for otra in ajenas):
                        rechazados.append(norm)
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO task_file_claims "
                        "(archivo, task_id, graph_id, project_slug, "
                        "tomado_at, vence_at) VALUES (?,?,?,?,?,?)",
                        (norm, task_id, graph_id, slug, ahora, vence))
                conn.commit()
                return rechazados
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    async def claim_task_files(self, task_id: str, graph_id: str,
                               archivos: list) -> list:
        """Toma los archivos para una tarea. Devuelve los que NO pudo.

        No falla si otro los tiene: devuelve la lista y quien llama
        decide. El orquestador no debería llegar acá con conflictos
        (`grafo.elegibles` ya los filtró), pero esta es la capa que de
        verdad garantiza, y una capa que garantiza tiene que poder decir
        que no. Ver `_claim_files_tx` por qué esto no es un `run()` más.
        """
        return await asyncio.to_thread(
            self._claim_files_tx, task_id, graph_id, archivos)

    async def renovar_claims(self, task_ids: list) -> int:
        """Corre el vencimiento de las reservas de tareas que siguen vivas.

        El latido de la lease. Lo llama el loop del orquestador con lo
        que tiene `en_curso`, o sea con las tareas que ese proceso
        está corriendo de verdad —no con las que la tabla *dice* que
        corren, que es justo la diferencia que hace útil todo esto—.

        Sin esto una tarea larga y viva perdería sus archivos al vencer
        el TTL, que es peor que el cuelgue que la lease arregla.
        """
        if not task_ids:
            return 0
        hueco = ",".join("?" for _ in task_ids)
        filas = await self.run(
            f"UPDATE task_file_claims SET vence_at=? "
            f"WHERE task_id IN ({hueco}) RETURNING archivo",
            (_vence_en(config.claim_ttl_s()), *task_ids))
        return len(filas)

    async def release_task_files(self, task_id: str) -> None:
        await self.run("DELETE FROM task_file_claims WHERE task_id=?",
                       (task_id,))

    async def files_claimed_by_others(self, task_id: str) -> set:
        """Archivos tomados por OTRA tarea viva. Va a `Permisos.reservadas`.

        `Permisos.para` ya está anclado a UN repo (el `project` de la
        tarea); si esto devolviera reservas de otros proyectos, el
        arreglo de `claim_task_files` (permitir la misma ruta relativa
        en dos proyectos) se anularía acá: la escritura rebotaría por
        una reserva ajena al repo que se está tocando. Se resuelve el
        `project_slug` de `task_id` vía su grafo; `''` si no resuelve
        (tarea o grafo inexistente, o grafo sin proyecto) — mismo
        default que usa `claim_task_files`.
        """
        filas = await self.run(
            "SELECT archivo FROM task_file_claims WHERE task_id<>? "
            "AND project_slug=COALESCE("
            "  (SELECT tg.project_slug FROM tasks t "
            "   JOIN task_graphs tg ON tg.id=t.graph_id WHERE t.id=?), '')",
            (task_id, task_id))
        return {f["archivo"] for f in filas}

    async def release_dead_claims(self, graph_id: Optional[str] = None) -> int:
        """Suelta las reservas de tareas que ya no están corriendo.

        Sin esto, un run que muere sin soltar (relay reiniciado, proceso
        matado) deja el archivo tomado para siempre y el grafo se traba
        sin que nadie pueda decir por qué.

        **El barrido es global a propósito**, aunque lo llame un grafo en
        particular. La regla es *barrer con al menos el alcance con que
        se bloquea*: si el barrido mirara solo las del grafo que arranca,
        una reserva que quedó de un grafo anterior —o de uno que después
        se borró, que no cascadea porque `task_file_claims` no tiene FK—
        bloquearía para siempre a todos los que vinieran. Barrer con
        MENOS alcance del que bloquea es justo el caso que deja el
        archivo tomado por nadie.

        Ojo, que la premisa cambió el 8/9/2026: `files_claimed_by_others`
        bloqueaba mirando TODAS las reservas vivas y ahora mira solo las
        del mismo proyecto. O sea que este barrido pasó a ser MÁS ancho
        que el bloqueo, no igual. Eso sigue siendo seguro —soltar de más
        no traba a nadie—, pero si alguna vez se achica el barrido, el
        límite ya no es "global": es el `project_slug`.

        Un `task_id` que ya no existe en `tasks` también se barre: es una
        reserva huérfana de un grafo borrado.

        **Y las que vencieron** (2026-09-04). La regla por estado sola no
        alcanzaba: `sanar()` cura las tareas DEL GRAFO QUE ARRANCA, pero
        `files_claimed_by_others` bloquea con las de TODOS. Un grafo que
        murió con nodos en `corriendo` retiene sus archivos para siempre
        —nadie lo va a sanar si no vuelve a correr— y traba a cualquier
        otro. Es la misma asimetría de alcance que arregla el párrafo de
        arriba, un nivel más arriba. No se arregla haciendo `sanar()`
        global: eso mataría las tareas de un grafo que SÍ está corriendo
        en paralelo. Hace falta distinguir vivo de huérfano, y la
        señal es el latido: una tarea viva renueva (`renovar_claims`),
        una huérfana no.

        Una reserva con `vence_at` NULL es anterior a la lease: no vence
        por reloj, solo la alcanza la regla por estado.

        `graph_id` queda solo para el log de quien pidió el barrido.
        """
        filas = await self.run(
            "DELETE FROM task_file_claims WHERE task_id NOT IN ("
            "  SELECT id FROM tasks WHERE estado='corriendo'"
            ") OR (vence_at IS NOT NULL AND vence_at < ?) "
            "RETURNING archivo", (now_iso(),))
        return len(filas)

    async def set_task_graph_state(self, graph_id: str, estado: str) -> None:
        await self.run(
            "UPDATE task_graphs SET estado=?, updated_at=? WHERE id=?",
            (estado, now_iso(), graph_id))

    async def set_task_graph_verificacion(self, graph_id: str,
                                          verificacion_json: str) -> None:
        """Guarda el veredicto de la verificacion de cierre del grafo.

        NO toca `updated_at`: ese campo lo mira
        `hay_turnos_humanos_despues` para decidir si el grafo sigue
        siendo lo que esta pasando en el hilo, y moverlo por escribir
        telemetria haria que un grafo viejo volviera a tapar la
        conversacion.
        """
        await self.run(
            "UPDATE task_graphs SET verificacion_json=? WHERE id=?",
            (verificacion_json, graph_id))
