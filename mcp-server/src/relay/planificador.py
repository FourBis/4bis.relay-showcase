"""De un pedido humano a un grafo de tareas ejecutable (el disparador).

F1 dio el grafo, F2 dio el motor que lo corre, y hasta hoy **no había
nadie que armara uno**: `correr_grafo` solo se llamaba desde los tests.
Este módulo es la pieza que faltaba — el pedido entra como texto y sale
como filas en `task_graphs` / `tasks`, listas para que el orquestador las
ejecute.

**Por qué un turno aparte y no el planificador de siempre.** El
planificador de `run_expert_staged` devuelve prosa: pasos numerados que
se le inyectan al ejecutor en el system prompt. Eso no se puede correr —
no tiene ids, ni dependencias, ni forma de saber qué toca cada paso. Acá
se pide **JSON con estructura**, que es lo único que un motor puede
tomar. Es el mismo modelo razonador; lo que cambia es qué se le pide.

**Lo que más importa de este archivo no es el prompt: es el parser.** Un
modelo que devuelve JSON lo devuelve envuelto en ```json, con un
preámbulo, con `deps` apuntando al título en vez de al id, o con ids que
inventó a mitad de camino. Todo eso llega, y todo eso hay que
enderezarlo antes de tocar la base. Por eso `parsear_tareas` es una
función pura, sin modelo ni DB: es donde se rompe de verdad y es donde
hay que poder probar cien casos feos sin gastar un token.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Optional

from . import grafo as G

logger = logging.getLogger("relay.planificador")

#: Tope de tareas por grafo. No es una limitación técnica: un plan de
#: cuarenta nodos es casi siempre un modelo que descompuso de más, y
#: revisarlo cuesta más que rehacerlo.
MAX_TAREAS = 20

#: Tope de salida del planificador. Va explícito porque el default del
#: proveedor NO alcanza para un grafo de veinte tareas: medido el
#: 8/9/2026 con Sonnet, 8 de 20 planes volvieron con el JSON cortado a
#: mitad de un string ("Unterminated string"), que `parsear_tareas`
#: rechaza entero. El síntoma es indistinguible de un modelo que
#: devuelve basura, y ahí se pierde el turno completo.
#:
#: 16384 y no 8192 porque con 8192 todavía se truncó uno. Verificado
#: contra minimax, nvidia y claude: los tres lo aceptan. Es un tope, no
#: un objetivo: un plan corto sigue costando lo que cuesta.
MAX_TOKENS_PLAN = 16384

INSTRUCCIONES = (
    "Eres el planificador de un equipo de bots programadores. Te dan un "
    "pedido grande y devuelves un grafo de tareas en JSON. Nada más: sin "
    "preámbulo, sin explicación, sin markdown alrededor."
)

# El último bullet de "Las reglas que importan" (estado final, no
# prohibiciones) sale de un A/B medido el 8/9/2026: el mismo pedido, el
# mismo modelo, editaba un CSS fuente y no corría el build cuando el
# pedido decía "no toques ningún otro archivo"; con "deja el repo en un
# estado consistente" sí lo corría. Pesó más que inyectar una skill que
# describía el paso de build textualmente.
PROMPT = """\
Convierte este pedido en un grafo de tareas ejecutable.

## El pedido
{objetivo}
{contexto}
## Formato exacto de la respuesta

{{"tareas": [
  {{"id": "t1",
   "titulo": "una línea, en infinitivo",
   "detalle": "qué hacer, con el detalle que necesita alguien que NO leyó el pedido",
   "deps": [],
   "archivos": ["ruta/relativa.py"],
   "idempotente": true}}
]}}

## Las reglas que importan

- **`deps` son ids de esta misma lista**, nunca títulos. Una tarea va en
  `deps` de otra solo si la segunda NO puede empezar hasta que la
  primera esté lista. No encadenes todo en fila: lo que puede correr en
  paralelo tiene que quedar sin dependencia entre sí, que es el punto.
- **Sin ciclos.** Si A depende de B, B no puede depender de A ni de nada
  que dependa de A.
- **`archivos`**: los que esa tarea va a ESCRIBIR, en rutas relativas al
  repo. Se usan para no correr dos tareas que escriben lo mismo, así que
  una lista de más serializa el plan sin necesidad y una de menos hace
  que dos bots se pisen. Si la tarea solo lee o solo corre comandos,
  déjala `[]`.
- **`idempotente`**: `true` solo si repetir la tarea entera desde cero da
  el mismo resultado. Correr tests, leer, analizar, regenerar un archivo
  completo: `true`. Migrar una base, abrir un PR, mandar algo, hacer un
  append: `false`. Ante la duda, `false` — un `true` mentiroso hace que
  el sistema reintente solo algo que ya tuvo efecto.
- Entre 2 y {max_tareas} tareas. Cada una tiene que ser un trabajo de
  minutos para un bot con acceso al repo, no un proyecto.
- El `detalle` es lo único que va a leer quien la ejecute: no tiene el
  pedido original ni ve las otras tareas.
- El `detalle` termina diciendo QUÉ TIENE QUE SER CIERTO cuando la tarea
  esté lista, no solo qué editar. Si el cambio necesita un paso derivado
  para tener efecto —recompilar, regenerar un artefacto, migrar, correr
  el test que lo cubre—, ese paso es parte de la tarea y va escrito. Y no
  agregues prohibiciones: una restricción de más ("no toques ningún otro
  archivo") apaga justo el paso que faltaba.

Devuelve SOLO el JSON.
"""

# ```json … ``` o ``` … ```: lo primero que hay que sacar, porque después
# el JSON queda igual de válido y el resto del parser no se entera.
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_ID_LIMPIO = re.compile(r"[^a-z0-9_.-]+")


def _bloque_json(texto: str) -> str:
    """El JSON de adentro de lo que sea que haya devuelto el modelo.

    Tres capas, de la más limpia a la más desesperada: el fence, el texto
    tal cual, y el recorte entre la primera llave y la última. La tercera
    existe porque el preámbulo ("Acá está el grafo:") es lo más común que
    devuelve un modelo al que le pediste que no lo pusiera.
    """
    if m := _FENCE.search(texto):
        return m.group(1).strip()
    t = texto.strip()
    if t.startswith("{") or t.startswith("["):
        return t
    inicio = t.find("{")
    fin = t.rfind("}")
    if inicio >= 0 and fin > inicio:
        return t[inicio:fin + 1]
    return t


def _id_valido(crudo: Any, i: int) -> str:
    s = _ID_LIMPIO.sub("_", str(crudo or "").strip().lower()).strip("_")
    return s[:40] or f"t{i + 1}"


def _lista_de_textos(v: Any) -> list[str]:
    """Lo que venga → lista de strings no vacíos.

    Un modelo escribe `"archivos": "src/db.py"` (string suelto) tan
    seguido como la lista bien formada, y `null` cuando no hay ninguno.
    """
    if v is None or isinstance(v, bool):
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x).strip() for x in v if str(x or "").strip()]


def parsear_tareas(texto: str, *, max_tareas: int = MAX_TAREAS) -> list[dict]:
    """El JSON del modelo → tareas listas para `create_task_graph`.

    Lanza `ValueError` si no hay nada rescatable. Lo que SÍ se arregla
    solo, porque es lo que pasa de verdad y rechazarlo sería tirar un
    grafo bueno por un detalle de forma:

    - el JSON envuelto en ```json, o con un preámbulo antes;
    - `{"tareas": [...]}` o la lista pelada;
    - ids feos, repetidos o ausentes (se normalizan y se numeran);
    - **`deps` con el título en vez del id** — el error más común del
      modelo, y el que más caro sale: `validar()` lo rechazaría como
      dependencia colgada y perderíamos el grafo entero por un nombre;
    - `deps` a una tarea que no existe: se descarta ESA arista, no la
      tarea. Un modelo que inventa un id igual ordenó bien el resto.
    """
    crudo = _bloque_json(texto or "")
    try:
        datos = json.loads(crudo)
    except (ValueError, TypeError) as e:
        raise ValueError(f"no devolvió JSON: {e}") from e

    if isinstance(datos, dict):
        for clave in ("tareas", "tasks", "nodos", "nodes"):
            if isinstance(datos.get(clave), list):
                datos = datos[clave]
                break
    if not isinstance(datos, list) or not datos:
        raise ValueError("el JSON no tiene una lista de tareas")

    brutas = [t for t in datos if isinstance(t, dict)][:max_tareas]
    if not brutas:
        raise ValueError("la lista no tiene tareas")

    # Primera pasada: ids definitivos. Hace falta completa antes de mirar
    # las deps, porque una dep puede apuntar a una tarea posterior.
    ids: list[str] = []
    usados: set = set()
    for i, t in enumerate(brutas):
        base = _id_valido(t.get("id"), i)
        cand, n = base, 2
        while cand in usados:            # id repetido: `validar` lo mata
            cand, n = f"{base}_{n}", n + 1
        usados.add(cand)
        ids.append(cand)

    # Índice para rescatar las deps escritas como título.
    por_titulo = {}
    for i, t in enumerate(brutas):
        clave = str(t.get("titulo") or t.get("title") or "").strip().lower()
        if clave:
            por_titulo.setdefault(clave, ids[i])

    salida = []
    for i, t in enumerate(brutas):
        deps = []
        for d in _lista_de_textos(t.get("deps") or t.get("depende_de")):
            crudo_d = d.strip()
            if (norm := _id_valido(crudo_d, i)) in usados:
                objetivo = norm
            elif (objetivo := por_titulo.get(crudo_d.lower(), "")) == "":
                logger.warning("dep %r de %r no existe: la descarto",
                               crudo_d, ids[i])
                continue
            if objetivo != ids[i] and objetivo not in deps:
                deps.append(objetivo)    # y nunca a sí misma
        salida.append({
            "id": ids[i],
            "titulo": (str(t.get("titulo") or t.get("title") or "").strip()
                       or ids[i])[:200],
            "detalle": str(t.get("detalle") or t.get("detail") or "").strip(),
            "deps": deps,
            "archivos": _lista_de_textos(t.get("archivos") or t.get("files")),
            # Default `False` a propósito, igual que en el grafo: un
            # campo que el modelo no puso no es un permiso para
            # reintentar solo algo que ya pudo tener efecto.
            "idempotente": bool(t.get("idempotente")),
            "orden": i,
        })
    return salida


def _ids_unicos(tareas: list[dict], graph_id: str) -> list[dict]:
    """Prefija los ids del modelo con el id del grafo. Deps incluidas.

    `tasks.id` es PRIMARY KEY **global** —el esquema lo dice: `t_<uuid8>`—
    pero el modelo numera `t1`, `t2`, … en cada plan. El primer grafo de
    la base entra bien y el segundo choca en la primera tarea con
    `UNIQUE constraint failed: tasks.id`.

    Medido el 24/8: el grafo `g_00000001` se guardó con sus doce tareas y
    el siguiente pedido creó `g_00000002` con CERO — y como
    `active_task_graph` devuelve igual ese grafo vacío, el hilo quedaba
    sin poder armar otro. Por eso se arregla acá y no pidiéndole ids
    únicos al modelo: un prompt no es una restricción.

    Prefijo y no uuid nuevo para que el id siga siendo legible en la base
    y en los logs (`g_00000002:t3` dice de qué grafo es).
    """
    return [
        {**t,
         "id": f"{graph_id}:{t['id']}",
         "deps": [f"{graph_id}:{d}" for d in (t.get("deps") or [])]}
        for t in tareas
    ]


#: Intentos de TRANSPORTE por vuelta (no de contenido). El endpoint
#: gratis de NVIDIA que corre el planner devuelve 500 cada tanto y
#: repetir la misma request alcanza; lo que NO alcanza es reintentar un
#: plan mal armado sin decirle qué estuvo mal, y eso lo cubren las
#: vueltas de `armar_grafo`.
REINTENTOS_TRANSPORTE = 3
_BACKOFF_S = (2.0, 5.0)

#: Tope de UN turno del planificador.
_TURNO_MAX_S = 180.0

#: Tope de TODA la planificación: las dos vueltas de `armar_grafo`, la
#: cascada entera y los reintentos de cada modelo comparten este
#: presupuesto. Sin él el peor caso se multiplica solo —3 modelos × 3
#: intentos × 180s, dos veces— y da casi una hora en la que el humano
#: ve un grafo que "se está armando" y no tiene idea de si sigue vivo.
#: Ese cuelgue ya nos hizo lanzar grafos duplicados sobre el mismo repo.
PLANIFICACION_MAX_S = 600.0


#: Errores que NO son del proveedor: son bugs nuestros. Ningún reintento
#: ni ningún cambio de modelo los arregla, y disfrazarlos de "el
#: planificador no contestó" hace que se diagnostiquen como modelo malo.
#: Medido el 8/9/2026 en la suite: un `TypeError` por un doble de test
#: desactualizado se comió 3 reintentos con backoff en cada uno de los 3
#: modelos de la cascada antes de rendirse. Con modelos de verdad eso
#: son nueve llamadas pagas para nada.
#:
#: `ValueError` NO está en la lista a propósito: el JSON mal armado del
#: modelo llega como ValueError y ése sí se arregla cambiando de modelo.
_BUGS_NUESTROS = (TypeError, AttributeError, NameError, ImportError,
                  KeyError, IndexError)


def _que_hacer(e: BaseException) -> str:
    """Qué hacer con un fallo: `reintentar`, `cambiar` o `abortar`.

    - `reintentar`: repetir la MISMA request al MISMO modelo. Solo vale
      para lo que puede salir distinto en cinco segundos — un 5xx, un
      408, un corte de red (sin `status_code`).
    - `cambiar`: bajar al siguiente modelo de la cascada.
    - `abortar`: no es del proveedor, es del relay. Cortar y decirlo.

    Los tres casos que van derecho a `cambiar`, y por qué:

    - **429**: es CUOTA, no un hipo. Insistirle al endpoint gratis que
      acaba de decir "ya no" devuelve 429 otra vez y encima gasta el
      backoff. Lo que destraba un 429 es otro modelo, no más paciencia.
    - **Otro 4xx**: la request no le sirve a ESTE modelo (nemotron corta
      con 400 si le mandás una imagen). Repetirla da lo mismo; otro
      modelo puede aceptarla.
    - **Timeout nuestro**: ya se comió los 180s enteros. Repetirlo
      triplica la espera para el mismo cuelgue.
    """
    import asyncio
    if isinstance(e, _BUGS_NUESTROS):
        return "abortar"
    if isinstance(e, asyncio.TimeoutError):
        return "cambiar"
    code = getattr(e, "status_code", None)
    if code is None:
        return "reintentar"
    if code == 429:
        return "cambiar"
    if code == 408 or code >= 500:
        return "reintentar"
    return "cambiar"


def cascada_planner(project: dict, modelo: str = "") -> list[str]:
    """Modelos a probar para planificar, en orden y sin repetidos.

    El primero es el de siempre (`planner_model`); los que siguen salen
    de `defaults_json.planner_fallback` (lista o separados por coma) o
    de `FOURBIS_PLANNER_FALLBACK`. Sin fallback configurado la lista
    tiene un solo elemento y todo se comporta como antes.

    Va aparte de `config.planner_model_spec()` a propósito: los otros
    consumidores de `planner_model` (el coordinador del grafo) esperan
    UN spec, y meterles una lista adentro del mismo campo los rompería.

    `graph_planner_model` (8/9/2026) existe porque cortar el grafo y
    planificar un turno son trabajos distintos con economías distintas.
    Medido: el grafo corre 3 veces por día y el por-turno 21, así que un
    modelo caro acá sale ~5 USD al mes y allá ~46. Y el grafo es el que
    lo justifica: un mal corte manda a cinco bots a hacer lo equivocado,
    mientras que un plan de turno flojo lo absorbe el ejecutor. Sin esta
    clave los dos leían `planner_model` y no se podía subir uno sin
    pagar el otro. Quien no la setea no cambia en nada.
    """
    from . import config

    defaults = project.get("defaults_json") or {}
    principal = (modelo or defaults.get("graph_planner_model")
                 or defaults.get("planner_model")
                 or config.planner_model_spec())
    crudo = defaults.get("planner_fallback")
    if crudo is None:
        crudo = os.environ.get("FOURBIS_PLANNER_FALLBACK", "")
    if isinstance(crudo, str):
        crudo = crudo.split(",")
    orden: list[str] = []
    for cand in [principal, *(crudo or [])]:
        cand = cand.strip() if isinstance(cand, str) else ""
        if cand and cand not in orden:
            orden.append(cand)
    return orden


async def _pedir_plan(specs: list[str], desde: int, pedido: str, experts,
                      limite: float = 0.0):
    """Pide el plan bajando por la cascada. `(respuesta, fallo, indice)`.

    `desde` es por cuál modelo arrancar: si la primera vuelta ya quemó
    GLM por cuota, la segunda no vuelve a intentarlo — el índice del que
    contestó viaja de vuelta para eso.

    `limite` es un instante de `time.monotonic()`, no una duración: el
    presupuesto es de TODA la planificación y las dos vueltas de
    `armar_grafo` comparten el mismo. `0.0` = sin tope (los tests).
    """
    import asyncio

    fallo: Optional[BaseException] = None
    i = max(0, desde)
    while i < len(specs):
        resto = (limite - time.monotonic()) if limite else _TURNO_MAX_S
        if resto <= 0:
            fallo = fallo or TimeoutError(
                f"la planificación pasó {PLANIFICACION_MAX_S:.0f}s")
            break
        spec = specs[i]
        agente = experts.Agent(experts.build_model(spec),
                               instructions=INSTRUCCIONES)
        for intento in range(REINTENTOS_TRANSPORTE):
            resto = (limite - time.monotonic()) if limite else _TURNO_MAX_S
            if resto <= 0:
                return None, TimeoutError(
                    f"la planificación pasó {PLANIFICACION_MAX_S:.0f}s"), i
            try:
                r = await asyncio.wait_for(
                    agente.run(pedido,
                               model_settings={"max_tokens": MAX_TOKENS_PLAN}),
                    timeout=min(_TURNO_MAX_S, resto))
                return r, None, i
            except Exception as e:  # noqa: BLE001 — sin plan no hay grafo
                fallo = e
                que = _que_hacer(e)
                if que == "abortar":
                    # No es del proveedor: es un bug del relay. Bajar por
                    # la cascada solo lo repite N veces y lo disfraza.
                    logger.error(
                        "el planner cortó por un error del relay (%s: %s); "
                        "no reintento ni cambio de modelo",
                        type(e).__name__, str(e)[:200])
                    return None, e, i
                if que == "cambiar" or intento == REINTENTOS_TRANSPORTE - 1:
                    break
                espera = _BACKOFF_S[min(intento, len(_BACKOFF_S) - 1)]
                if limite and limite - time.monotonic() <= espera:
                    return None, TimeoutError(
                        "no queda presupuesto para reintentar dentro del "
                        f"tope de {PLANIFICACION_MAX_S:.0f}s"), i
                logger.warning(
                    "el planner %s falló (%s: %s); reintento en %.0fs (%d/%d)",
                    spec, type(e).__name__, str(e)[:200], espera,
                    intento + 2, REINTENTOS_TRANSPORTE)
                await asyncio.sleep(espera)
        i += 1
        if i < len(specs):
            logger.warning("el planner %s no sirvió (%s): bajo a %s", spec,
                           type(fallo).__name__, specs[i])
    return None, fallo, i


async def armar_grafo(
    project: dict, objetivo: str, *, db: Any,
    modelo: str = "", contexto: str = "",
    conversation_id: str = "", max_tareas: int = MAX_TAREAS,
) -> dict:
    """Pide el grafo, lo endereza, lo valida y lo guarda. Devuelve el grafo.

    Dos intentos, y el segundo lleva el error del primero. No es un
    reintento a ciegas: los errores que tira `validar` están escritos
    para que un modelo pueda arreglarlos —el del ciclo trae el camino
    entero— y un modelo que ve "hay un ciclo: t2 → t3 → t2" corta la
    arista correcta. Reintentar sin decirle qué estuvo mal da el mismo
    grafo roto la segunda vez.
    """
    from . import experts

    specs = cascada_planner(project, modelo)
    ctx = f"\n## Contexto\n{contexto.strip()}\n" if contexto.strip() else "\n"
    prompt = PROMPT.format(objetivo=objetivo.strip(), contexto=ctx,
                           max_tareas=max_tareas)

    # Abrimos el chat ANTES del bucle de la cascada: ese paso tarda
    # >3 min y no crea ninguna fila. Sin este rastro, desde afuera el
    # planificador parece colgado y se termina lanzando un grafo
    # duplicado sobre el mismo repo.
    chat_id = ""
    if hasattr(db, "create_chat"):
        chat_id = await db.create_chat(
            project_slug=project.get("slug"),
            source="planificador",
            author="planificador",
            target=project.get("slug"),
            conversation_id=conversation_id or None,
            user_prompt=objetivo.strip(),
        )

    try:
        ultimo = ""
        usado = 0                      # por qué modelo de la cascada arrancar
        # Un solo presupuesto para las dos vueltas y la cascada entera.
        limite = time.monotonic() + PLANIFICACION_MAX_S
        for vuelta in range(2):
            pedido = prompt if vuelta == 0 else (
                f"{prompt}\n\n## Tu respuesta anterior no sirvió\n{ultimo}\n\n"
                "Devuelve el JSON corregido, solo el JSON.")
            r, fallo, usado = await _pedir_plan(specs, usado, pedido, experts,
                                                limite=limite)
            if r is None:
                # Decir CUÁL de los dos pasó. "No contestó ningún modelo"
                # ante un bug nuestro manda a revisar proveedores durante
                # horas; el mensaje es parte del arreglo, no adorno.
                if _que_hacer(fallo) == "abortar":
                    raise RuntimeError(
                        f"el planificador se cortó por un error del relay "
                        f"({type(fallo).__name__}: {fallo}). Cambiar de "
                        f"modelo no lo arregla.") from fallo
                raise RuntimeError(
                    f"el planificador no contestó en ninguno de {len(specs)} "
                    f"modelo(s) ({type(fallo).__name__}: {fallo})") from fallo
            try:
                tareas = parsear_tareas(getattr(r, "output", "") or "",
                                        max_tareas=max_tareas)
                if len(tareas) < 2:
                    raise ValueError(
                        "un grafo de una sola tarea no es un grafo: si el "
                        "pedido entra en un run, no hace falta descomponerlo")
                G.validar([G.Nodo(id=t["id"], titulo=t["titulo"],
                                  deps=tuple(t["deps"])) for t in tareas])
            except (ValueError, G.GrafoInvalido) as e:
                ultimo = str(e)
                logger.warning("grafo inválido (vuelta %d): %s",
                               vuelta + 1, ultimo)
                continue
            graph_id = f"g_{uuid.uuid4().hex[:8]}"
            g = await db.create_task_graph(
                graph_id, objetivo.strip(),
                tareas=_ids_unicos(tareas, graph_id),
                conversation_id=conversation_id or None,
                project_slug=project.get("slug"))
            logger.info("grafo %s armado: %d tareas (%s)", graph_id,
                        len(tareas), specs[usado])
            if chat_id:
                await db.finish_chat(chat_id, status="ok", model=specs[usado])
            return g
        raise RuntimeError(
            f"el planificador no pudo armar un grafo válido: {ultimo}")
    except Exception as e:
        if chat_id and hasattr(db, "finish_chat"):
            await db.finish_chat(chat_id, status="error", error=str(e))
        raise


def resumen(g: dict) -> str:
    """El grafo en markdown, para el mensaje que ve el humano.

    Va por capas y no por lista numerada con "_después de: …_".

    El formato viejo repetía el título de CADA dependencia en cada
    línea, así que la última tarea arrastraba las once anteriores y el
    mensaje entero se leía como un menú para elegir por dónde empezar —
    justo lo contrario de lo que dice arriba ("las estoy ejecutando").
    Pasó con un humano de verdad el 24/8: leyó opciones donde el grafo
    ya llevaba dos tareas hechas.

    Una capa es lo que puede correr a la vez (ver `grafo.capas`), así
    que agrupar por capa dice lo mismo que las dependencias sin
    repetirlas, y de paso muestra lo único que la lista numerada no
    podía mostrar: qué es secuencia obligada y qué es simultáneo.
    """
    por_id = {t["id"]: t for t in g["tasks"]}
    nodos = [G.Nodo.desde_fila(t, t.get("deps") or ()) for t in g["tasks"]]
    try:
        filas = G.capas(nodos)
    except G.GrafoInvalido:
        # Un grafo que no valida no debería llegar acá (`armar_grafo` lo
        # valida antes de guardar), pero el resumen es un texto: que no
        # se pueda dibujar no puede tumbar la respuesta al humano.
        filas = [[t["id"] for t in sorted(g["tasks"],
                                          key=lambda x: (x["orden"], x["id"]))]]

    # Capas consecutivas de UNA tarea son una cadena, no tandas: un
    # "Después" por cada una repite la palabra sin agregar nada. Se
    # juntan, pero marcadas como secuenciales — decir "2 en paralelo" de
    # dos tareas encadenadas sería mentir sobre lo que va a pasar.
    grupos: list[list] = []            # [[paralelo?, [ids]], ...]
    for capa in filas:
        paralelo = len(capa) > 1
        if grupos and not paralelo and not grupos[-1][0]:
            grupos[-1][1].extend(capa)
        else:
            grupos.append([paralelo, list(capa)])

    lineas: list[str] = []
    n = 0
    for i, (paralelo, ids) in enumerate(grupos):
        if i == 0:
            cabecera = "**Arrancan ahora**" if paralelo else "**Arranca ahora**"
        elif paralelo:
            cabecera = f"**Después** ({len(ids)} en paralelo)"
        elif len(ids) > 1:
            cabecera = "**Después, en orden**"
        else:
            cabecera = "**Después**"
        lineas.append(f"\n{cabecera}")
        for tid in ids:
            n += 1
            lineas.append(f"{n}. {por_id[tid]['titulo']}")
    return "\n".join(lineas).strip()
