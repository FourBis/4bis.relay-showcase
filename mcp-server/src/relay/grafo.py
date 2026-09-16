"""El plan como grafo de tareas (F1, 2026-08-17).

Hasta hoy el plan del planificador era un **string** inyectado en el
system prompt del ejecutor. Ahí moría: no se podía saber en qué paso
iba, ni retomarlo tras un reinicio, ni mostrarlo. Cuando el usuario
pidió "que se vea el avance en la UI", el problema no era de UI — no
había nada que mostrar.

Este módulo es la lógica pura del grafo: **sin DB, sin LLM, sin red**.
Esa separación es a propósito. Lo que decide qué corre, qué se bloquea y
qué se reintenta es lo que más caro sale si está mal, y es justamente lo
que no se puede probar bien si está enredado con `await db.run(...)` y
con un modelo contestando. Acá entran dicts y salen decisiones.

**Por qué un DAG y no una lista.** Dos cosas que una lista no da:

1. Paralelismo real — tres nodos sin dependencias entre sí pueden correr
   a la vez, y con MiniMax por nodo eso es barato.
2. **Bloqueo correcto.** Si el nodo "migrar el esquema" falla, "cargar
   los datos" no debe intentarse: no es que falle, es que ya no tiene
   sentido. Con una lista, o parás todo (perdés el trabajo paralelo que
   sí servía) o seguís sobre una base rota.

**Los estados y por qué estos.**

    pendiente        le faltan dependencias, o nadie lo tomó todavía
    corriendo        hay un run vivo
    hecho            terminó bien
    fallado          lo intentó y no pudo (ya sin reintentos)
    bloqueado        una dependencia falló — no es su culpa y no se intenta
    esperando_humano paró a preguntar (`ask_human`)

`bloqueado` separado de `fallado` no es cosmética: son dos cosas
distintas para el humano (uno se arregla, el otro se destraba solo
cuando arreglás al de arriba) y para las métricas (contar bloqueados
como fallas infla el número y esconde la causa real).
"""
from __future__ import annotations

import dataclasses
from typing import Iterable, Optional

PENDIENTE = "pendiente"
CORRIENDO = "corriendo"
HECHO = "hecho"
FALLADO = "fallado"
BLOQUEADO = "bloqueado"
ESPERANDO = "esperando_humano"

TERMINALES = frozenset({HECHO, FALLADO, BLOQUEADO})
ESTADOS = frozenset({PENDIENTE, CORRIENDO, HECHO, FALLADO, BLOQUEADO, ESPERANDO})

#: Fases de fin de turno que significan **no terminó su trabajo**.
#:
#: Vive acá, en el módulo puro, porque estaba duplicada y las copias se
#: desincronizaron. El orquestador consideraba roto solo lo que muere de
#: golpe (error, timeout, cancelled) y el panel además contaba
#: `budget_exceeded` y `off_plan`: la MISMA tarea salía verde en el grafo
#: y roja en la lista de turnos.
#:
#: Lo caro no era la inconsistencia sino a qué lado se equivocaba el
#: grafo. Medido en code-hero-rpg (31/8): de siete tareas, la que
#: corregía los bugs críticos murió por `provider_error` y la de
#: estabilidad por `budget_exceeded` tras 255 tool calls — y las dos
#: quedaron `hecho`. El grafo declaró 7/7 sobre un juego al que le
#: faltaba justo eso, y el humano lo notó antes que el sistema:
#: "¿queda algo pendiente?".
#:
#: Un turno cortado dejó trabajo a medio hacer; el historial se rescata
#: y se puede continuar, pero eso lo decide un humano, no un `hecho`
#: puesto por default.
FASES_INCOMPLETAS = frozenset({
    "error", "timeout", "hard_timeout", "idle_timeout", "cancelled",
    "no_result", "budget_exceeded", "provider_error", "off_plan",
    # 2026-09-02: tope de subdivisión (Fase 1) — el nodo agotó las
    # tandas permitidas con trabajo real en curso. Reanudable, pero NO
    # `hecho`: el consejo que deja es partir la tarea en lotes.
    "budget_split",
})


class GrafoInvalido(ValueError):
    """El grafo no se puede ejecutar tal como vino."""


@dataclasses.dataclass(frozen=True)
class Nodo:
    """Una tarea. `deps` son ids de los que tienen que estar `hecho` antes."""

    id: str
    titulo: str
    deps: tuple = ()
    estado: str = PENDIENTE
    idempotente: bool = False
    intentos: int = 0
    max_intentos: int = 2
    detalle: str = ""
    orden: int = 0
    # Archivos que la tarea dice que va a tocar. Los declara el
    # planificador y sirven para NO correr en paralelo dos nodos que
    # escriben lo mismo. Una declaracion de un LLM miente, asi que esto
    # es la capa de planificacion; el cumplimiento real esta en
    # `files.Permisos.reservadas` (ver docs/GRAFO_DE_TAREAS.md).
    archivos: tuple = ()
    parent_id: Optional[str] = None

    @classmethod
    def desde_fila(cls, fila: dict, deps: Iterable[str] = ()) -> "Nodo":
        import json as _json
        crudo = fila.get("archivos") or "[]"
        try:
            archivos = tuple(_json.loads(crudo)) if isinstance(crudo, str) \
                else tuple(crudo)
        except (ValueError, TypeError):
            archivos = ()
        return cls(
            id=fila["id"], titulo=fila.get("titulo") or "",
            deps=tuple(deps), estado=fila.get("estado") or PENDIENTE,
            idempotente=bool(fila.get("idempotente")),
            intentos=int(fila.get("intentos") or 0),
            max_intentos=int(fila.get("max_intentos") or 2),
            detalle=fila.get("detalle") or "", orden=int(fila.get("orden") or 0),
            archivos=archivos, parent_id=fila.get("parent_id"),
        )


def validar(nodos: list[Nodo]) -> None:
    """Lanza `GrafoInvalido` si no se puede ejecutar.

    Se valida al GUARDAR y no al ejecutar, a propósito: el grafo lo
    escribe un LLM, y un LLM puede inventar una dependencia a un id que
    no existe o cerrar un ciclo. Descubrirlo tres nodos después deja el
    grafo a medio correr; descubrirlo al guardar deja pedirle otro.
    """
    ids = [n.id for n in nodos]
    if not ids:
        raise GrafoInvalido("el grafo no tiene tareas")
    dups = {i for i in ids if ids.count(i) > 1}
    if dups:
        raise GrafoInvalido(f"ids repetidos: {', '.join(sorted(dups))}")
    conocidos = set(ids)
    for n in nodos:
        for d in n.deps:
            if d not in conocidos:
                raise GrafoInvalido(
                    f"la tarea {n.id!r} depende de {d!r}, que no existe")
        if d_self := [d for d in n.deps if d == n.id]:
            raise GrafoInvalido(f"la tarea {n.id!r} depende de sí misma")
        if n.estado not in ESTADOS:
            raise GrafoInvalido(f"estado desconocido en {n.id!r}: {n.estado!r}")
    ciclo = _buscar_ciclo(nodos)
    if ciclo:
        raise GrafoInvalido("hay un ciclo: " + " → ".join(ciclo))


def _buscar_ciclo(nodos: list[Nodo]) -> list[str]:
    """El ciclo como lista de ids, o `[]`. DFS con marcas de color.

    Devuelve el camino y no un bool porque el mensaje va a un LLM que
    tiene que corregir el grafo: "hay un ciclo" no le dice cuál romper.
    """
    deps = {n.id: list(n.deps) for n in nodos}
    estado: dict = {}          # 0 = sin visitar, 1 = en la pila, 2 = cerrado
    camino: list[str] = []

    def dfs(v: str) -> list[str]:
        estado[v] = 1
        camino.append(v)
        for w in deps.get(v, ()):
            if estado.get(w, 0) == 1:
                return camino[camino.index(w):] + [w]
            if estado.get(w, 0) == 0:
                if r := dfs(w):
                    return r
        estado[v] = 2
        camino.pop()
        return []

    for n in nodos:
        if estado.get(n.id, 0) == 0:
            if r := dfs(n.id):
                return r
    return []


def orden_topologico(nodos: list[Nodo]) -> list[str]:
    """Ids en un orden en el que cada uno va después de sus dependencias.

    Kahn, con desempate por `orden` y después por id: el mismo grafo
    tiene que dar SIEMPRE la misma lista. Sin desempate estable, la UI
    reordena los nodos entre refrescos y parece que pasó algo.
    """
    validar(nodos)
    por_id = {n.id: n for n in nodos}
    pendientes = {n.id: set(n.deps) for n in nodos}
    salida: list[str] = []
    while pendientes:
        listos = sorted(
            (i for i, d in pendientes.items() if not d),
            key=lambda i: (por_id[i].orden, i))
        if not listos:                        # validar() ya lo descartó
            raise GrafoInvalido("ciclo detectado al ordenar")
        for i in listos:
            salida.append(i)
            del pendientes[i]
        for restantes in pendientes.values():
            restantes.difference_update(listos)
    return salida


def capas(nodos: list[Nodo]) -> list[list[str]]:
    """El grafo en filas: cada capa es lo que puede correr a la vez.

    Es lo que hace dibujable al DAG. Una capa = los nodos cuya cadena
    más larga de dependencias mide lo mismo, así que **estar en la misma
    fila ES la definición de poder correr en paralelo**: un humano
    mirando el dibujo ve de un vistazo qué es secuencia obligada y qué
    es trabajo simultáneo, que es justamente lo que una lista numerada
    no puede mostrar.

    Se usa el camino MÁS LARGO y no el más corto a propósito: con el más
    corto, un nodo que depende de uno temprano y de uno tardío subiría a
    la fila del temprano y su flecha apuntaría hacia atrás. Con el más
    largo, toda arista va de una fila a otra posterior — sin eso, el
    dibujo tiene flechas cruzadas que no significan nada.

    Vive acá y no en el JavaScript por lo mismo que `orden_topologico`:
    es lógica de grafo, y la lógica de grafo se prueba en Python.
    """
    validar(nodos)
    por_id = {n.id: n for n in nodos}
    nivel: dict = {}
    for nid in orden_topologico(nodos):        # las deps ya están resueltas
        deps = [d for d in por_id[nid].deps if d in nivel]
        nivel[nid] = 1 + max((nivel[d] for d in deps), default=-1)
    salida: list[list[str]] = [[] for _ in range(max(nivel.values(), default=-1) + 1)]
    for nid, lvl in nivel.items():
        salida[lvl].append(nid)
    # Mismo desempate que el orden topológico: el mismo grafo tiene que
    # dibujarse SIEMPRE igual, o la UI baraja los nodos entre refrescos y
    # parece que pasó algo.
    return [sorted(fila, key=lambda i: (por_id[i].orden, i)) for fila in salida]


def listas(nodos: list[Nodo]) -> list[Nodo]:
    """Las tareas que se pueden lanzar AHORA.

    Pendientes con todas sus dependencias en `hecho`. Devuelve varias
    porque el punto del DAG es poder lanzarlas juntas; quien llama decide
    cuántas toma según su presupuesto de paralelismo.
    """
    por_id = {n.id: n for n in nodos}
    salida = [
        n for n in nodos
        if n.estado == PENDIENTE
        and all(por_id[d].estado == HECHO for d in n.deps if d in por_id)
    ]
    return sorted(salida, key=lambda n: (n.orden, n.id))


def bloqueados_por(nodos: list[Nodo], fallado_id: str) -> list[str]:
    """Los que hay que marcar `bloqueado` porque `fallado_id` no está.

    Transitivo: si C depende de B y B de A, un fallo en A bloquea a los
    dos. Se calcula sobre las aristas y no "los que sigan en la lista",
    que es lo que haría una lista ordenada — y bloquearía de más.
    """
    hijos: dict = {}
    for n in nodos:
        for d in n.deps:
            hijos.setdefault(d, []).append(n.id)
    vistos: set = set()
    pila = list(hijos.get(fallado_id, ()))
    por_id = {n.id: n for n in nodos}
    while pila:
        actual = pila.pop()
        if actual in vistos:
            continue
        vistos.add(actual)
        pila.extend(hijos.get(actual, ()))
    # Lo ya terminado no se re-marca: un nodo que alcanzó a terminar bien
    # antes del fallo hizo su trabajo, y pisarlo borraría el resultado.
    return sorted(i for i in vistos
                  if i in por_id and por_id[i].estado not in TERMINALES)


def decidir_tras_fallo(nodo: Nodo) -> str:
    """`reintentar` | `preguntar` | `fallar`. La regla que acordamos.

    Reintento automático **solo** para nodos declarados idempotentes.
    Reintentar algo que ya escribió archivos, corrió una migración o
    abrió un PR puede duplicar el efecto, y el runtime no tiene forma de
    saber si el efecto ya ocurrió: el que sabe es quien escribió la
    tarea. Por eso `idempotente` lo declara el planificador y por eso el
    default es `False` — ante la duda, para y pregunta.
    """
    if nodo.intentos >= nodo.max_intentos:
        return "fallar"
    return "reintentar" if nodo.idempotente else "preguntar"


def sustituidos(nodos: list[Nodo]) -> set[str]:
    """Padres cerrados cuyo trabajo fue reemplazado por subtareas.

    `add_tasks_to_graph(reemplaza=...)` conserva el padre para auditoría,
    guarda `parent_id` en los hijos y reapunta sus dependientes. Si ese
    reapunte falta, el padre sigue siendo operativo: ocultarlo podría
    convertir una dependencia fallida en un avance ficticio.
    """
    padres = {n.parent_id for n in nodos if n.parent_id and n.parent_id != n.id}
    referenciados = {d for n in nodos for d in n.deps}
    # El alta normal apunta a un padre preexistente y no forma ciclos.
    # Una relación histórica corrupta tampoco debe esconder fallos: se
    # recorren los parent_id una vez, sin recursión, admitiendo splits anidados.
    linaje = {n.id: n.parent_id for n in nodos}
    vistos: set[str] = set()
    ciclicos: set[str] = set()
    for inicio in linaje:
        camino: dict[str, int] = {}
        actual = inicio
        while actual in linaje and actual not in vistos:
            vistos.add(actual)
            camino[actual] = len(camino)
            actual = linaje[actual]
        if actual in camino:
            ciclicos.update(list(camino)[camino[actual]:])
    return {n.id for n in nodos
            if n.id in padres and n.estado in TERMINALES
            and n.id not in referenciados and n.id not in ciclicos}


def progreso(nodos: list[Nodo]) -> dict:
    """Avance operativo; los padres sustituidos quedan contados aparte."""
    reemplazados = sustituidos(nodos)
    conteo = {e: 0 for e in ESTADOS}
    for n in nodos:
        if n.id not in reemplazados:
            conteo[n.estado] = conteo.get(n.estado, 0) + 1
    total = len(nodos) - len(reemplazados)
    cerrados = sum(conteo[e] for e in TERMINALES)
    return {
        "total": total,
        "total_historico": len(nodos),
        "sustituidos": len(reemplazados),
        "hechos": conteo[HECHO],
        "corriendo": conteo[CORRIENDO],
        "pendientes": conteo[PENDIENTE],
        "bloqueados": conteo[BLOQUEADO],
        "fallados": conteo[FALLADO],
        "esperando_humano": conteo[ESPERANDO],
        "porcentaje": round(100 * cerrados / total) if total else 0,
        "estado": estado_del_grafo(nodos),
    }


def estado_del_grafo(nodos: list[Nodo]) -> str:
    """`activo` | `hecho` | `fallado` | `trabado`.

    `trabado` es el que importa y el que una lista no puede expresar:
    no queda nada corriendo ni lanzable, pero tampoco terminó todo. Sin
    este estado, un grafo con un fallo arriba se queda "activo" para
    siempre y nadie se entera de que ya no avanza.
    """
    reemplazados = sustituidos(nodos)
    nodos = [n for n in nodos if n.id not in reemplazados]
    if not nodos:
        return "hecho"
    if any(n.estado == ESPERANDO for n in nodos):
        return "activo"
    if all(n.estado == HECHO for n in nodos):
        return "hecho"
    if any(n.estado == CORRIENDO for n in nodos) or listas(nodos):
        return "activo"
    if any(n.estado in (FALLADO, BLOQUEADO) for n in nodos):
        return "fallado"
    return "trabado"


def estado_visible(nodos: list[Nodo], crudo: str = "") -> str:
    """Etiqueta para MOSTRAR, no el estado de la máquina (P8, 6/9/26).

    `estado_del_grafo` devuelve `activo` tanto si hay un nodo corriendo
    de verdad como si el grafo está parado esperando que un humano
    conteste una `ask_human` — y esa mezcla ahí es correcta: la
    reanudación (`graphs_resume` y `_retomar_grafo_tras_respuesta` en
    server.py) necesita que el grafo siga siendo `activo` para poder
    relanzarlo, así que ESE valor no se toca.

    El problema es de lectura: un humano mirando el panel (o cualquier
    consulta por estado) no puede distinguir "corre solo" de "quedó
    esperando una decisión, puede tardar días". Medido el 6/9: seis
    grafos en `activo` con 4 a 5 días parados, todos con una tarea en
    `esperando_humano` y ninguna `corriendo`.

    Esta función deriva SOLO la etiqueta que ve el humano; no persiste
    nada ni agrega un estado a la máquina. Única, para que el endpoint
    y el panel no puedan desincronizarse mostrando cosas distintas.
    """
    # `cancelado` no sale de los nodos: cancelar es una decisión del
    # humano que vive en `task_graphs.estado` (ver el esquema en
    # `db.py`: activo|hecho|fallado|cancelado) y deja las tareas como
    # estaban. Sin esto un grafo cancelado se sigue mostrando "activo",
    # que es el mismo engaño que esta función viene a sacar. Es un
    # defecto PREEXISTENTE —el panel ya derivaba de los nodos y nunca
    # miró `task_graphs.estado`—, medido el 6/9 con el grafo `demo`
    # cancelado ese mismo día.
    if crudo == "cancelado":
        return crudo
    base = estado_del_grafo(nodos)
    if (base == "activo"
            and any(n.estado == ESPERANDO for n in nodos)
            and not any(n.estado == CORRIENDO for n in nodos)):
        return ESPERANDO
    return base


#: Fases de `FASES_INCOMPLETAS` que cortan por agotar presupuesto, no
#: por un bug del ejecutor. La diferencia importa porque el arreglo es
#: distinto —subir un límite o partir el trabajo, no debuggear código—
#: y hoy se ven igual: las dos terminan la tarea en `fallado`.
#: `orquestador` (ver `_cerrar_nodo_del_run`) ya escribe el texto exacto
#: en `tasks.error` (`f"el nodo terminó en {fase!r}"`); acá solo se
#: reconoce ese texto — no se inventa nomenclatura nueva.
FASES_POR_PRESUPUESTO = frozenset({"budget_exceeded", "budget_split"})


def es_error_de_presupuesto(error: str) -> bool:
    """`True` si `error` (columna `tasks.error`) dice que cortó por presupuesto.

    Caso anonimizado: dos nodos de un grafo de inventorydemo murieron así el 1/9 y el
    grafo quedó `fallado` para siempre sin que nada dijera por qué —
    "la tarea falló" y "se acabó el presupuesto" son cosas distintas, y
    hoy se ven igual.
    """
    txt = error or ""
    return any(f"'{f}'" in txt for f in FASES_POR_PRESUPUESTO)


# ---------- concurrencia: dos bots no tocan el mismo archivo ----------
#
# El DAG dice qué PUEDE correr; esto dice qué puede correr **junto**. Son
# dos preguntas distintas: dos nodos sin dependencia entre sí son
# lanzables a la vez según el grafo, y aun así no deben correr juntos si
# escriben el mismo archivo — el segundo pisaría al primero y el diff
# final sería de nadie.
#
# Ojo con lo que esto NO es: una garantía. Es planificación sobre lo que
# el planificador DECLARÓ, y una declaración de un LLM puede quedarse
# corta. La garantía la da `files.Permisos.reservadas`, que rechaza la
# escritura de un archivo reservado por otra tarea viva. Mismo criterio
# que el sandbox: acá se evita el choque, allá se impide.


def norm_archivo(p: str) -> str:
    """Ruta comparable: separadores unix, sin `./`, minúsculas.

    Minúsculas porque el caso real es Windows, donde `Src/Main.py` y
    `src/main.py` son EL MISMO archivo. Comparar con mayúsculas dejaría
    pasar a dos tareas sobre el mismo archivo por una diferencia que el
    filesystem ni registra.
    """
    s = (p or "").strip().replace("\\", "/").strip("/")
    while s.startswith("./"):
        s = s[2:]
    return s.lower()


def _pisa(a: str, b: str) -> bool:
    """¿`a` y `b` son el mismo archivo, o uno es carpeta del otro?

    Declarar una carpeta (`src/relay/`) tiene que chocar con un archivo
    de adentro: si no, una tarea que dice "toco todo src/relay" correría
    en paralelo con otra que toca `src/relay/db.py`.
    """
    if a == b:
        return True
    return b.startswith(a + "/") or a.startswith(b + "/")


def conflictan(uno: Nodo, otro: Nodo) -> bool:
    """¿Dos nodos que escriben? Entonces no pueden correr juntos.

    Hasta el 9/9/2026 esto comparaba las listas de `archivos` y solo
    frenaba a los que se pisaban. Esa precisión era falsa: la reserva
    por archivo la respetan las tools de archivo (`Permisos.reservadas`)
    pero NO la `shell`, que corre un comando arbitrario y puede escribir
    cualquier cosa. Un nodo con la reserva de `src/a.py` no tenía nada
    que le impidiera hacer `sed -i` sobre el `src/b.py` de su hermano,
    así que la exclusión terminaba dependiendo de que el agente se
    portara bien.

    Filtrar comandos de shell no es una opción, y sacarle la shell a los
    nodos con hermanos vivos rompe el caso normal —el nodo de
    verificación existe para correr el build—. Queda serializar a los
    que declaran que van a escribir: dos escritores nunca comparten el
    working tree, y el precio es paralelismo entre nodos que escriben,
    que es justo donde no lo querés.

    Un nodo SIN `archivos` sigue sin conflictuar con nadie: es el que
    solo lee o corre comandos, y tratarlo como conflictivo serializaría
    el grafo entero y perdería el punto del DAG. Ojo que eso apoya en el
    contrato del planificador —`archivos` son los que va a ESCRIBIR—:
    un nodo que miente y escribe con `[]` declarado sigue pudiendo
    pisar. Cerrar eso pide worktree por nodo, no otra regla acá.

    `_pisa` no se va: sigue decidiendo el choque carpeta/archivo en las
    reservas de la base (`claim_task_files`), que es la capa que protege
    a las tools de archivo.
    """
    a = any(str(x).strip() for x in uno.archivos)
    b = any(str(x).strip() for x in otro.archivos)
    return a and b


def elegibles(nodos: list[Nodo], *, tope: int = 2,
              en_curso: Iterable[Nodo] = ()) -> list[Nodo]:
    """Qué lanzar ahora: hasta `tope`, sin pisarse archivos entre sí.

    El orden importa y es el del grafo (`listas`): si dos candidatos
    chocan, gana el primero y el otro espera al turno siguiente. No se
    reordena para "meter más": eso haría que el mismo grafo avance
    distinto según el azar, y con un humano mirando la UI eso se ve como
    que el sistema hace lo que quiere.
    """
    corriendo = list(en_curso)
    elegidos: list[Nodo] = []
    for cand in listas(nodos):
        if len(elegidos) + len(corriendo) >= max(1, tope):
            break
        if any(conflictan(cand, otro) for otro in corriendo + elegidos):
            continue
        elegidos.append(cand)
    return elegidos
