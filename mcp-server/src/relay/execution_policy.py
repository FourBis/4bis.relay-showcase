"""Permisos efectivos del run, heredados por sus tareas de asyncio.

Existe porque `read_only` y `rutas_vedadas` se aplicaban SOLO a las tools
de archivos: la `shell` se habilitaba por su cuenta y corría comandos sin
enterarse de ninguna de las dos, y el rol HTTP del usuario no llegaba a
`db_query`, que decidía mirando nada más la conexión configurada. Un
proyecto marcado para revisión perdía la restricción apenas el agente
cambiaba de herramienta.

El `ContextVar` es lo que hace que el rol sobreviva al salto de la
request HTTP a la task de background: `run_expert` corre despues de que
el endpoint contestó 202, y para entonces el request ya no existe.
"""
from contextvars import ContextVar
from dataclasses import dataclass


request_role: ContextVar[str] = ContextVar("relay_request_role", default="owner")


@dataclass(frozen=True)
class ExecutionPolicy:
    read_only: bool
    sql_read_only: bool
    unrestricted_tools: bool
    shell_allowed: bool

    @classmethod
    def for_run(cls, defaults: dict, reserved=(), *, code_write=False, role=None):
        """Politica de un run. `reserved` NO restringe: ver abajo.

        `unrestricted_tools=False` saca los MCP externos del run.
        `shell_allowed` permite comandos de código a Dev asignados;
        no concede escritura SQL ni operaciones de cuentas externas.
        Las restricciones que siempre se conservan son las dos
        cosas que de verdad son un limite de confianza:

        - `read_only` / `rutas_vedadas` del proyecto: la shell no sabe
          respetarlas —no hay forma de acotarle las rutas a un comando
          arbitrario—, asi que la unica opcion honesta es no dartela.
        - un rol sin asignación explícita: el permiso del humano debe
          valer aunque el agente cambie de tool.

        `reserved` son los archivos que OTRA tarea del mismo grafo tiene
        tomados. Eso es coordinacion, no confianza: dice "no escribas
        estas rutas", y de eso ya se encarga `Permisos.reservadas` en
        las tools de archivos. Si ademas apagara la shell, cualquier
        nodo que corre en paralelo con un hermano perderia `npm run
        build` y `pytest` — y el nodo de verificacion, que existe justo
        para correr el build, es el que mas seguido tiene hermanos
        vivos. Se romperia el caso normal de un grafo sano.

        Ojo con lo que ESTO no cubre y donde se cubre: la shell si puede
        escribir un archivo reservado por otro nodo, porque corre un
        comando arbitrario y no hay como acotarle las rutas. Eso no se
        arregla aca sino en el planificador de tandas: desde el 9/9/2026
        `grafo.conflictan` serializa a todos los nodos que declaran
        `archivos`, asi que nunca hay dos escritores sobre el mismo
        working tree. Si algun dia se afloja aquella regla, este agujero
        se reabre.
        """
        member = (role or request_role.get()) != "owner"
        read_only = bool(defaults.get("read_only")) or (member and not code_write)
        feedback = bool(defaults.get("task_feedback"))
        restricted = read_only or member or feedback or bool(defaults.get("rutas_vedadas"))
        shell = not (read_only or feedback or bool(defaults.get("rutas_vedadas")))
        return cls(read_only, read_only or member or feedback, not restricted, shell)

    @classmethod
    async def for_project(cls, db, project: dict, reserved=()):
        from . import identity, user_accounts
        role = request_role.get()
        actor = user_accounts.current_actor.get()
        granted = role == "owner"
        if actor and actor[1] != "owner":
            rows = await db.list_users() if db is not None else []
            user = next((row for row in rows if row["email"] == actor[1]), {})
            role = user.get("role", "unregistered") if user.get("enabled", True) else "disabled"
            granted = identity.user_can_write_project(user, project.get("slug", ""))
        if project.get("_task_mode") == "write" and not granted:
            raise RuntimeError("Ya no tienes permiso de escritura en este proyecto; revisa la asignación en Equipo.")
        return cls.for_run(project.get("defaults_json") or {}, reserved,
                           code_write=granted, role=role)
