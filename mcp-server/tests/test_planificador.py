"""El disparador: de un pedido humano a un grafo ejecutable (2026-08-23).

F1 dio el grafo y F2 el motor, pero hasta hoy nadie armaba uno:
`correr_grafo` solo se llamaba desde los tests. Esto prueba la pieza que
faltaba.

Casi todo el archivo es sobre el **parser**, no sobre el prompt. Ahí es
donde esto se rompe en producción: el modelo devuelve JSON envuelto en
```json, con un preámbulo, con `deps` apuntando al título en vez del id,
o con ids que inventó. Cada caso de acá es una forma real en que un LLM
contesta mal una consigna que entendió bien — y tirar el grafo entero
por eso sería perder trabajo bueno por un detalle de forma.
"""
from __future__ import annotations

import json

import pytest

from relay import grafo, planificador
from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


@pytest.fixture(autouse=True)
def _sin_fallback_del_entorno(monkeypatch):
    """La cascada cae al entorno cuando el proyecto no define fallback.

    Sin esto, el `.env` de la máquina decide si estos tests pasan: el
    30/8 configurar un respaldo real —para que un timeout no se lleve
    puesto el grafo— rompió tres tests que ni mencionan la variable. El
    que sí la prueba la setea él mismo, después de este fixture.
    """
    monkeypatch.delenv("FOURBIS_PLANNER_FALLBACK", raising=False)


def _json(tareas):
    return json.dumps({"tareas": tareas}, ensure_ascii=False)


# ---------- el parser: lo que el modelo devuelve de verdad ----------


def test_el_caso_feliz():
    t = planificador.parsear_tareas(_json([
        {"id": "t1", "titulo": "Leer el esquema", "idempotente": True},
        {"id": "t2", "titulo": "Migrar", "deps": ["t1"],
         "archivos": ["src/db.py"]},
    ]))
    assert [x["id"] for x in t] == ["t1", "t2"]
    assert t[1]["deps"] == ["t1"]
    assert t[1]["archivos"] == ["src/db.py"]
    assert t[0]["idempotente"] is True and t[1]["idempotente"] is False


def test_viene_envuelto_en_un_fence():
    crudo = "```json\n" + _json([
        {"id": "a", "titulo": "Uno"}, {"id": "b", "titulo": "Dos"}]) + "\n```"
    assert len(planificador.parsear_tareas(crudo)) == 2


def test_viene_con_preambulo_aunque_le_dijimos_que_no():
    """Lo más común: el modelo entiende la consigna y igual saluda."""
    crudo = ("¡Claro! Acá está el grafo que armé:\n\n"
             + _json([{"id": "a", "titulo": "Uno"},
                      {"id": "b", "titulo": "Dos"}])
             + "\n\nAvisame si querés que lo ajuste.")
    assert len(planificador.parsear_tareas(crudo)) == 2


def test_la_lista_pelada_sin_la_clave_tareas():
    crudo = json.dumps([{"id": "a", "titulo": "Uno"},
                        {"id": "b", "titulo": "Dos"}])
    assert len(planificador.parsear_tareas(crudo)) == 2


def test_las_deps_por_titulo_se_resuelven_al_id():
    """El error más caro del modelo: `validar()` lo rechazaría como
    dependencia colgada y perderíamos el grafo entero por un nombre."""
    t = planificador.parsear_tareas(_json([
        {"id": "t1", "titulo": "Migrar el esquema"},
        {"id": "t2", "titulo": "Cargar datos", "deps": ["Migrar el esquema"]},
    ]))
    assert t[1]["deps"] == ["t1"]


def test_una_dep_inventada_se_descarta_sin_tirar_la_tarea():
    """Un modelo que inventa un id igual ordenó bien el resto: se pierde
    ESA arista, no el trabajo."""
    t = planificador.parsear_tareas(_json([
        {"id": "t1", "titulo": "Uno"},
        {"id": "t2", "titulo": "Dos", "deps": ["t1", "t99"]},
    ]))
    assert t[1]["deps"] == ["t1"]


def test_los_ids_repetidos_se_desambiguan():
    t = planificador.parsear_tareas(_json([
        {"id": "t1", "titulo": "Uno"}, {"id": "t1", "titulo": "Dos"}]))
    assert [x["id"] for x in t] == ["t1", "t1_2"]


def test_sin_ids_se_numeran():
    t = planificador.parsear_tareas(_json([
        {"titulo": "Uno"}, {"titulo": "Dos"}]))
    assert [x["id"] for x in t] == ["t1", "t2"]


def test_un_id_con_espacios_y_mayusculas_se_normaliza():
    t = planificador.parsear_tareas(_json([
        {"id": "Migrar DB", "titulo": "Uno"}, {"id": "b", "titulo": "Dos"}]))
    assert t[0]["id"] == "migrar_db"


def test_una_tarea_que_depende_de_si_misma_pierde_esa_arista():
    """`validar()` la mataría; y no es una intención del modelo, es un
    descuido al copiar el id."""
    t = planificador.parsear_tareas(_json([
        {"id": "t1", "titulo": "Uno", "deps": ["t1"]},
        {"id": "t2", "titulo": "Dos"}]))
    assert t[0]["deps"] == []


def test_archivos_como_string_suelto():
    t = planificador.parsear_tareas(_json([
        {"id": "a", "titulo": "Uno", "archivos": "src/db.py"},
        {"id": "b", "titulo": "Dos", "archivos": None}]))
    assert t[0]["archivos"] == ["src/db.py"] and t[1]["archivos"] == []


def test_el_tope_de_tareas_recorta():
    t = planificador.parsear_tareas(
        _json([{"id": f"t{i}", "titulo": str(i)} for i in range(50)]),
        max_tareas=5)
    assert len(t) == 5


def test_lo_que_no_es_json_se_rechaza():
    with pytest.raises(ValueError, match="no devolvió JSON"):
        planificador.parsear_tareas("Perdón, no puedo ayudarte con eso.")


def test_json_valido_sin_tareas_se_rechaza():
    with pytest.raises(ValueError, match="lista de tareas"):
        planificador.parsear_tareas('{"resultado": "ok"}')


def test_el_grafo_que_sale_del_parser_es_valido_para_el_motor():
    """El contrato real: lo que devuelve el parser tiene que pasar
    `validar()` sin tocarlo. Si no, el arreglo del parser no sirvió."""
    t = planificador.parsear_tareas(_json([
        {"id": "T 1", "titulo": "Migrar", "deps": ["Migrar"]},
        {"id": "t2", "titulo": "Cargar", "deps": ["Migrar", "fantasma"]},
        {"id": "t2", "titulo": "Otra"},
    ]))
    grafo.validar([grafo.Nodo(id=x["id"], titulo=x["titulo"],
                              deps=tuple(x["deps"])) for x in t])


# ---------- armar_grafo: el turno del modelo + la persistencia ----------


class _Salida:
    def __init__(self, texto):
        self.output = texto


def _modelo_que_dice(*respuestas):
    """Un `Agent` falso que contesta una cosa distinta por vuelta."""
    quedan = list(respuestas)
    vistos = []

    class _Agente:
        def __init__(self, *a, **kw):
            pass

        async def run(self, prompt, **kw):
            _Agente.ultimo_kw = kw
            vistos.append(prompt)
            return _Salida(quedan.pop(0))

    return _Agente, vistos


async def test_arma_y_guarda_el_grafo(db, monkeypatch):
    from relay import experts

    agente, _ = _modelo_que_dice(_json([
        {"id": "t1", "titulo": "Leer", "idempotente": True},
        {"id": "t2", "titulo": "Escribir", "deps": ["t1"],
         "archivos": ["src/a.py"]},
    ]))
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    g = await planificador.armar_grafo(
        {"slug": "demo"}, "hacé todo", db=db, conversation_id="conv1")

    assert g["objetivo"] == "hacé todo"
    assert g["conversation_id"] == "conv1" and g["project_slug"] == "demo"
    # Los ids del modelo (`t1`, `t2`) van prefijados con el grafo:
    # `tasks.id` es PK global y el modelo numera igual en cada plan.
    # Ver `_ids_unicos` y `test_dos_grafos_seguidos_no_se_pisan_los_ids`.
    assert [t["id"] for t in g["tasks"]] == [f"{g['id']}:t1", f"{g['id']}:t2"]
    assert g["tasks"][1]["deps"] == [f"{g['id']}:t1"]
    # y quedó en la base, no solo en memoria
    assert (await db.get_task_graph(g["id"]))["tasks"][0]["titulo"] == "Leer"


async def test_un_grafo_invalido_se_reintenta_con_el_error_adentro(db, monkeypatch):
    """Reintentar a ciegas da el mismo grafo roto. El error de `validar`
    está escrito para que un modelo lo pueda arreglar —el del ciclo trae
    el camino entero— así que viaja en el segundo pedido."""
    from relay import experts

    ciclo = _json([{"id": "t1", "titulo": "Uno", "deps": ["t2"]},
                   {"id": "t2", "titulo": "Dos", "deps": ["t1"]}])
    bueno = _json([{"id": "t1", "titulo": "Uno"},
                   {"id": "t2", "titulo": "Dos", "deps": ["t1"]}])
    agente, vistos = _modelo_que_dice(ciclo, bueno)
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    g = await planificador.armar_grafo({"slug": "demo"}, "x", db=db)

    assert len(vistos) == 2
    assert "ciclo" in vistos[1] and "t1" in vistos[1]
    assert len(g["tasks"]) == 2


class _Http(Exception):
    """Imita el `ModelHTTPError` de pydantic-ai: lo que miramos es el
    `status_code`, no la clase (importarla acopla el planner al SDK)."""

    def __init__(self, status_code: int):
        super().__init__(f"status_code: {status_code}, body: Internal Server Error")
        self.status_code = status_code


def _modelo_que_falla(*resultados):
    """Agent falso donde un item puede ser una excepción a lanzar."""
    quedan = list(resultados)
    vistos = []

    class _Agente:
        def __init__(self, *a, **kw):
            pass

        async def run(self, prompt, **kw):
            _Agente.ultimo_kw = kw
            vistos.append(prompt)
            r = quedan.pop(0)
            if isinstance(r, BaseException):
                raise r
            return _Salida(r)

    return _Agente, vistos


async def test_un_500_del_proveedor_se_reintenta_igual(db, monkeypatch):
    """El endpoint gratis de NVIDIA tira 500 cada tanto. Repetir la MISMA
    request alcanza — antes el primer 500 mataba la planificación entera
    aunque el loop de dos vueltas estuviera justo ahí."""
    from relay import experts

    bueno = _json([{"id": "t1", "titulo": "Uno"},
                   {"id": "t2", "titulo": "Dos", "deps": ["t1"]}])
    agente, vistos = _modelo_que_falla(_Http(500), bueno)
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())
    monkeypatch.setattr(planificador, "_BACKOFF_S", (0.0, 0.0))

    g = await planificador.armar_grafo({"slug": "demo"}, "x", db=db)

    assert len(g["tasks"]) == 2
    # el reintento manda el MISMO pedido: no hubo respuesta que corregir
    assert len(vistos) == 2 and vistos[0] == vistos[1]
    assert "no sirvió" not in vistos[1]


async def test_un_500_no_gasta_la_vuelta_de_correccion(db, monkeypatch):
    """Un fallo de transporte y un plan inválido son cosas distintas: el
    500 se repite en el lugar, y la corrección del ciclo sigue estando."""
    from relay import experts

    ciclo = _json([{"id": "t1", "titulo": "Uno", "deps": ["t2"]},
                   {"id": "t2", "titulo": "Dos", "deps": ["t1"]}])
    bueno = _json([{"id": "t1", "titulo": "Uno"},
                   {"id": "t2", "titulo": "Dos", "deps": ["t1"]}])
    agente, vistos = _modelo_que_falla(_Http(503), ciclo, bueno)
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())
    monkeypatch.setattr(planificador, "_BACKOFF_S", (0.0, 0.0))

    g = await planificador.armar_grafo({"slug": "demo"}, "x", db=db)

    assert len(g["tasks"]) == 2
    assert len(vistos) == 3
    assert "ciclo" in vistos[2]          # la corrección llegó igual


async def test_un_400_no_se_reintenta(db, monkeypatch):
    """Un 400 es la request en sí (nemotron corta con 400 si le mandás
    una imagen): mandarla tres veces da tres 400 y triplica la espera."""
    from relay import experts

    agente, vistos = _modelo_que_falla(_Http(400), _Http(400), _Http(400))
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    with pytest.raises(RuntimeError, match="no contestó"):
        await planificador.armar_grafo({"slug": "demo"}, "x", db=db)
    assert len(vistos) == 1


async def test_el_timeout_nuestro_no_se_reintenta(db, monkeypatch):
    """Ya se comió los 180s: repetirlo triplica la espera del mismo
    cuelgue. Lo que se reintenta es lo que vuelve rápido y distinto."""
    import asyncio

    from relay import experts

    agente, vistos = _modelo_que_falla(asyncio.TimeoutError(),
                                       asyncio.TimeoutError())
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    with pytest.raises(RuntimeError, match="no contestó"):
        await planificador.armar_grafo({"slug": "demo"}, "x", db=db)
    assert len(vistos) == 1


def _modelo_por_spec(guion):
    """Agent falso que contesta segun el spec con el que lo construyeron.

    `guion` es {spec: [resultado, ...]}; un resultado que es excepcion se
    lanza. Registra el orden real de specs usados, que es lo que la
    cascada tiene que respetar."""
    usados = []

    class _Agente:
        def __init__(self, modelo, **kw):
            self.spec = modelo
            usados.append(modelo)

        async def run(self, prompt, **kw):
            _Agente.ultimo_kw = kw
            r = guion[self.spec].pop(0)
            if isinstance(r, BaseException):
                raise r
            return _Salida(r)

    return _Agente, usados


_BUENO = _json([{"id": "t1", "titulo": "Uno"},
                {"id": "t2", "titulo": "Dos", "deps": ["t1"]}])


def test_la_cascada_arma_el_orden_sin_repetidos():
    """El principal primero; los fallback despues, por coma o por lista,
    y sin duplicar el que ya esta."""
    p = {"defaults_json": {"planner_model": "a", "planner_fallback": "b, c ,a"}}
    assert planificador.cascada_planner(p) == ["a", "b", "c"]
    p2 = {"defaults_json": {"planner_model": "a", "planner_fallback": ["b"]}}
    assert planificador.cascada_planner(p2) == ["a", "b"]
    # sin fallback: un solo modelo, todo se comporta como antes
    p3 = {"defaults_json": {"planner_model": "a"}}
    assert planificador.cascada_planner(p3) == ["a"]
    # el argumento explicito gana sobre el del proyecto
    assert planificador.cascada_planner(p2, "z") == ["z", "b"]


def test_la_cascada_toma_el_fallback_del_entorno(monkeypatch):
    monkeypatch.setenv("FOURBIS_PLANNER_FALLBACK", "x,y")
    p = {"defaults_json": {"planner_model": "a"}}
    assert planificador.cascada_planner(p) == ["a", "x", "y"]


async def test_un_429_cambia_de_modelo_sin_insistir(db, monkeypatch):
    """Un 429 es CUOTA: insistirle al mismo endpoint devuelve 429 otra vez.
    Lo que destraba es el siguiente modelo, y sin gastar el backoff."""
    from relay import experts

    agente, usados = _modelo_por_spec({"glm": [_Http(429)], "nemo": [_BUENO]})
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: spec)

    g = await planificador.armar_grafo(
        {"slug": "demo", "defaults_json": {"planner_model": "glm",
                                           "planner_fallback": "nemo"}},
        "x", db=db)

    assert len(g["tasks"]) == 2
    # UN intento con glm (no tres) y despues el siguiente
    assert usados == ["glm", "nemo"]


async def test_la_correccion_no_vuelve_al_modelo_quemado(db, monkeypatch):
    """Si glm se quedo sin cuota en la primera vuelta, la vuelta de
    correccion arranca del que contesto: volver al quemado es regalar
    otro 429."""
    from relay import experts

    ciclo = _json([{"id": "t1", "titulo": "Uno", "deps": ["t2"]},
                   {"id": "t2", "titulo": "Dos", "deps": ["t1"]}])
    agente, usados = _modelo_por_spec({"glm": [_Http(429)],
                                       "nemo": [ciclo, _BUENO]})
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: spec)

    g = await planificador.armar_grafo(
        {"slug": "demo", "defaults_json": {"planner_model": "glm",
                                           "planner_fallback": "nemo"}},
        "x", db=db)

    assert len(g["tasks"]) == 2
    assert usados == ["glm", "nemo", "nemo"]


async def test_un_500_reintenta_en_el_mismo_antes_de_bajar(db, monkeypatch):
    """El 5xx si es un hipo: se repite en el lugar. Recien cuando se
    agotan los intentos baja al siguiente."""
    from relay import experts

    guion = {"nemo": [_Http(500), _Http(500), _Http(500)], "glm": [_BUENO]}
    agente, usados = _modelo_por_spec(guion)
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: spec)
    monkeypatch.setattr(planificador, "_BACKOFF_S", (0.0, 0.0))

    g = await planificador.armar_grafo(
        {"slug": "demo", "defaults_json": {"planner_model": "nemo",
                                           "planner_fallback": "glm"}},
        "x", db=db)

    assert len(g["tasks"]) == 2
    # nemo se comio los TRES intentos (el guion quedo vacio) antes de bajar;
    # `usados` cuenta un Agent por modelo, no por intento.
    assert guion["nemo"] == []
    assert usados == ["nemo", "glm"]


async def test_si_toda_la_cascada_falla_lo_dice(db, monkeypatch):
    from relay import experts

    agente, usados = _modelo_por_spec({"a": [_Http(429)], "b": [_Http(429)]})
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: spec)

    with pytest.raises(RuntimeError, match="ninguno de 2 modelo"):
        await planificador.armar_grafo(
            {"slug": "demo", "defaults_json": {"planner_model": "a",
                                               "planner_fallback": "b"}},
            "x", db=db)
    assert usados == ["a", "b"]
    assert await db.run("SELECT * FROM task_graphs") == []


async def test_dos_intentos_fallidos_no_dejan_un_grafo_a_medias(db, monkeypatch):
    from relay import experts

    basura = "no puedo"
    agente, _ = _modelo_que_dice(basura, basura)
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    with pytest.raises(RuntimeError, match="no pudo armar"):
        await planificador.armar_grafo({"slug": "demo"}, "x", db=db)
    assert await db.run("SELECT * FROM task_graphs") == []


async def test_una_sola_tarea_no_es_un_grafo(db, monkeypatch):
    """Si el pedido entra en un run, descomponerlo es puro costo."""
    from relay import experts

    una = _json([{"id": "t1", "titulo": "Uno"}])
    agente, _ = _modelo_que_dice(una, una)
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    with pytest.raises(RuntimeError):
        await planificador.armar_grafo({"slug": "demo"}, "x", db=db)


def test_el_resumen_agrupa_por_capa_y_no_repite_dependencias():
    """El mensaje tiene que leerse como "esto ya arrancó", no como un menú.

    El formato viejo colgaba "_después de: <todos los títulos>_" de cada
    línea, así que la última tarea arrastraba las once anteriores. Un
    humano lo leyó como opciones para elegir mientras el grafo ya
    llevaba dos tareas hechas (24/8).
    """
    g = {"tasks": [
        {"id": "t1", "titulo": "Migrar", "deps": [], "orden": 0},
        {"id": "t2", "titulo": "Sembrar", "deps": [], "orden": 1},
        {"id": "t3", "titulo": "Cargar", "deps": ["t1", "t2"], "orden": 2}]}
    texto = planificador.resumen(g)

    assert "después de" not in texto, "volvió el ruido que parecía un menú"
    # Lo que arranca junto se ve junto, y lo que espera se ve separado.
    assert "**Arrancan ahora**" in texto and "**Después**" in texto
    assert texto.index("Migrar") < texto.index("Cargar")
    assert texto.index("**Después**") < texto.index("Cargar")
    # La numeración sigue siendo corrida sobre todas las tareas.
    assert "3. Cargar" in texto


def test_el_resumen_no_se_cae_con_un_grafo_que_no_valida():
    """Es un texto para el humano: no puede tumbar la respuesta."""
    g = {"tasks": [
        {"id": "t1", "titulo": "Uno", "deps": ["t2"], "orden": 0},
        {"id": "t2", "titulo": "Dos", "deps": ["t1"], "orden": 1}]}
    texto = planificador.resumen(g)
    assert "Uno" in texto and "Dos" in texto


# ---------- el disparador dentro de un hilo abierto (2026-08-23) ----------


async def test_un_pedido_nuevo_en_un_hilo_abierto_arma_grafo(db, monkeypatch):
    """El agujero que dejaba `task_graphs` vacía en producción.

    `DEMASIADO_GRANDE:` armaba grafo solo fuera de un hilo, y todo
    pedido que entra por una conversación abierta es follow-up: en la
    base no había UN grafo después de semanas de uso. Ahora el corte lo
    decide `_es_continuacion` (el mensaje), no `is_followup` (el hilo).
    """
    from relay import experts, server

    agente, _ = _modelo_que_dice(_json([
        {"id": "t1", "titulo": "Inventariar la UI", "archivos": ["a.md"]},
        {"id": "t2", "titulo": "Capturar el login", "deps": ["t1"],
         "archivos": ["b.md"]},
    ]))
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())
    largados: list[str] = []
    monkeypatch.setattr(server, "_largar_grafo",
                        lambda app, proj, gid: largados.append(gid))

    salida = await server._grafo_en_vez_de_proponer(
        app={}, db=db, project={"slug": "demo", "defaults_json": {}},
        user="Genera un manual de usuario con capturas de todos los pasos",
        conv_id="conv-abierta",
        result={"plan": "DEMASIADO_GRANDE:\n1. Inventariar\n2. Capturar",
                "phase_at_end": "planned", "content": "propuesta"})

    assert salida["phase_at_end"] == "graph"
    assert largados == [salida["graph_id"]]
    guardado = await db.get_task_graph(salida["graph_id"])
    assert guardado["conversation_id"] == "conv-abierta"
    assert [t["titulo"] for t in guardado["tasks"]] == [
        "Inventariar la UI", "Capturar el login"]


async def test_un_plan_vivo_frena_al_segundo_y_lo_dice(db, monkeypatch):
    """Dos planes sobre el mismo repo se pisan, así que el segundo no
    arranca. Lo que faltaba era DECIRLO: la respuesta salía como una
    propuesta cualquiera ("todavía no ejecuté nada") y el humano no
    tenía cómo saber que lo único que faltaba era esperar. Pasó el 30/8
    a las 07:25, después de que el planificador gastara 17k tokens."""
    from relay import server

    await db.create_task_graph("g_vivo", "lo que ya corre", tareas=[
        {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}],
        conversation_id="conv-ocupada", project_slug="demo")
    app = {server.GRAFOS_KEY: {"g_vivo": object()}}   # lo corre este proceso

    salida = await server._grafo_en_vez_de_proponer(
        app=app, db=db, project={"slug": "demo", "defaults_json": {}},
        user="otro pedido grande", conv_id="conv-ocupada",
        result={"plan": "DEMASIADO_GRANDE:\n1. Algo", "content": "propuesta",
                "phase_at_end": "planned"})

    assert salida["phase_at_end"] == "planned"     # no se ejecutó nada
    assert "graph_id" not in salida
    assert "ya hay un plan corriendo" in salida["content"]
    assert "0 de 2" in salida["content"]


async def test_un_plan_colgado_no_frena_al_hilo_para_siempre(db, monkeypatch):
    """Un grafo que quedó `activo` sin nadie corriéndolo, sin nodos
    vivos y sin preguntas abiertas no protege nada: solo deja la
    conversación sin poder volver a planificar. Hay uno así desde el
    24/8 — tres nodos en `esperando_humano` con todas sus preguntas ya
    contestadas."""
    from relay import experts, server

    await db.create_task_graph("g_colgado", "abandonado", tareas=[
        {"id": "a", "titulo": "A"}], conversation_id="conv-trabada",
        project_slug="demo")
    await db.update_task("g_colgado:a", estado="esperando_humano")

    agente, _ = _modelo_que_dice(_json([
        {"id": "t1", "titulo": "Uno"},
        {"id": "t2", "titulo": "Dos", "deps": ["t1"]}]))
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())
    monkeypatch.setattr(server, "_largar_grafo", lambda app, proj, gid: None)

    salida = await server._grafo_en_vez_de_proponer(
        app={}, db=db, project={"slug": "demo", "defaults_json": {}},
        user="un pedido nuevo y grande", conv_id="conv-trabada",
        result={"plan": "DEMASIADO_GRANDE:\n1. Algo", "content": "propuesta",
                "phase_at_end": "planned"})

    assert salida["phase_at_end"] == "graph"


# ---------- dos grafos en la misma base (2026-08-24) ----------


async def test_dos_grafos_seguidos_no_se_pisan_los_ids(db, monkeypatch):
    """El bug que hacía que el SEGUNDO grafo naciera vacío.

    `tasks.id` es PRIMARY KEY global (el esquema pide `t_<uuid8>`) pero
    el modelo numera `t1`, `t2`, … en cada plan. El primer grafo entraba
    bien y el segundo chocaba en la primera tarea con
    `UNIQUE constraint failed: tasks.id`. Como el INSERT del grafo iba en
    su propia transacción, quedaba un grafo `activo` con CERO tareas —
    y ese grafo vacío traba el hilo, porque `active_task_graph` lo
    devuelve y no deja armar otro.
    """
    from relay import experts

    plan = _json([{"id": "t1", "titulo": "Uno"},
                  {"id": "t2", "titulo": "Dos", "deps": ["t1"]}])
    agente, _ = _modelo_que_dice(plan, plan, plan, plan)
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    g1 = await planificador.armar_grafo({"slug": "demo"}, "uno", db=db)
    g2 = await planificador.armar_grafo({"slug": "demo"}, "dos", db=db)

    assert len(g1["tasks"]) == 2
    assert len(g2["tasks"]) == 2, "el segundo grafo nació vacío"
    assert g1["id"] != g2["id"]
    # Los ids llevan el grafo adelante, así que no pueden chocar.
    assert {t["id"] for t in g1["tasks"]}.isdisjoint(
        {t["id"] for t in g2["tasks"]})
    # Y las deps se remapearon con ellos: si no, apuntan a la nada y el
    # orquestador no lanza nunca la segunda tarea.
    dep = next(t for t in g2["tasks"] if t["deps"])
    assert dep["deps"][0] in {t["id"] for t in g2["tasks"]}


async def test_un_grafo_a_medio_guardar_no_queda_en_la_base(db, monkeypatch):
    """O entran todas las tareas o no entra el grafo.

    El contrapeso del test de arriba: aunque el choque de ids ya no
    pase, cualquier fallo a mitad del guardado no puede dejar un grafo
    `activo` sin tareas — es lo que traba el hilo.
    """
    from relay import experts

    plan = _json([{"id": "t1", "titulo": "Uno"},
                  {"id": "t2", "titulo": "Dos", "deps": ["t1"]}])
    agente, _ = _modelo_que_dice(plan, plan)
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    # Falla en la SEGUNDA sentencia: el grafo ya "entró", las tareas no.
    original = db.run_tx

    async def rompe(sentencias):
        await original(sentencias[:1] + [("INSERT INTO tasks (id) VALUES (?)",
                                          (None,))])

    monkeypatch.setattr(db, "run_tx", rompe)
    with pytest.raises(Exception):
        await planificador.armar_grafo({"slug": "demo"}, "x", db=db)

    assert await db.run("SELECT * FROM task_graphs") == [], \
        "quedó un grafo huérfano que traba el hilo"


async def test_el_plan_pide_max_tokens_explicito(db, monkeypatch):
    """Sin esto el planner usa el default del proveedor y trunca.

    Medido el 8/9/2026: con el default, 8 de 20 planes de Sonnet
    volvieron con el JSON cortado a mitad de un string. `parsear_tareas`
    los rechaza enteros, y el sintoma —"no devolvio JSON"— es igual al
    de un modelo que escupe basura, asi que se diagnostica mal.
    """
    from relay import experts

    agente, _ = _modelo_que_dice(_json([
        {"id": "t1", "titulo": "Leer", "idempotente": True},
        {"id": "t2", "titulo": "Escribir", "deps": ["t1"]},
    ]))
    monkeypatch.setattr(experts, "Agent", agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    await planificador.armar_grafo(
        {"slug": "demo"}, "hace algo grande", db=db, conversation_id="c1")

    kw = getattr(agente, "ultimo_kw", {})
    assert kw.get("model_settings", {}).get("max_tokens") == \
        planificador.MAX_TOKENS_PLAN, (
        "el planner corrio sin max_tokens: vuelve a truncar con Sonnet")


def test_el_grafo_puede_tener_su_propio_modelo():
    """`graph_planner_model` gana sobre `planner_model` solo en el grafo.

    Los dos planificadores leian la misma clave, asi que subir el del
    grafo a Sonnet (3/dia, ~5 USD/mes) arrastraba tambien al por-turno
    (21/dia, ~46 USD/mes). Sin la clave nueva, nada cambia.
    """
    caro = "claude:claude-sonnet-5"
    barato = "minimax:MiniMax-M3"

    solo_turno = {"defaults_json": {"planner_model": barato}}
    assert planificador.cascada_planner(solo_turno)[0] == barato

    partido = {"defaults_json": {"planner_model": barato,
                                 "graph_planner_model": caro}}
    assert planificador.cascada_planner(partido)[0] == caro, (
        "el grafo sigue atado al modelo del planificador por turno")

    # Un argumento explicito sigue ganando sobre las dos claves.
    assert planificador.cascada_planner(partido, modelo=barato)[0] == barato


# ---------- 8/9/2026: un bug nuestro no es un fallo del proveedor ----------


def test_los_bugs_del_relay_no_se_reintentan():
    """Un TypeError no lo arregla ni el backoff ni otro proveedor.

    Antes caian en `reintentar` por no tener `status_code`. `ValueError`
    queda afuera a proposito: el JSON mal armado del modelo llega asi, y
    ese si se arregla cambiando de modelo.
    """
    class Http(Exception):
        def __init__(self, c):
            self.status_code = c

    for e in (TypeError("x"), AttributeError("x"), KeyError("x"),
              NameError("x"), IndexError("x"), ImportError("x")):
        assert planificador._que_hacer(e) == "abortar", type(e).__name__
    assert planificador._que_hacer(ValueError("json roto")) != "abortar"
    assert planificador._que_hacer(Http(429)) == "cambiar"
    assert planificador._que_hacer(Http(503)) == "reintentar"


async def test_un_bug_nuestro_no_recorre_la_cascada(db, monkeypatch):
    """Medido: 3 reintentos x 3 modelos = 9 llamadas pagas para nada.

    En la suite del 8/9/2026 un doble desactualizado tiraba TypeError y
    el planificador lo trataba como "el proveedor no contesta": bajaba
    por toda la cascada y despues reportaba "no contesto en ninguno de 3
    modelo(s)", que manda a revisar proveedores durante horas.
    """
    from relay import experts

    llamadas = []

    class _Agente:
        def __init__(self, *a, **kw):
            pass

        async def run(self, prompt, **kw):
            llamadas.append(1)
            raise TypeError("run() got an unexpected keyword argument")

    monkeypatch.setattr(experts, "Agent", _Agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    proyecto = {"slug": "demo", "defaults_json": {
        "planner_model": "a:1", "planner_fallback": "b:2,c:3"}}
    assert len(planificador.cascada_planner(proyecto)) == 3

    with pytest.raises(RuntimeError) as ex:
        await planificador.armar_grafo(
            proyecto, "hace algo", db=db, conversation_id="c1")

    assert len(llamadas) == 1, (
        f"un bug nuestro recorrio la cascada: {len(llamadas)} llamadas")
    assert "error del relay" in str(ex.value), (
        "el mensaje sigue culpando al proveedor")


async def test_la_planificacion_tiene_un_tope_total(monkeypatch):
    """El presupuesto es de TODA la planificacion, no por turno.

    Sin esto el peor caso se multiplica solo: 3 modelos x 3 intentos x
    180s, dos vueltas. Casi una hora con el humano mirando un grafo que
    "se esta armando".
    """
    from relay import experts

    llamadas = []

    class _Agente:
        def __init__(self, *a, **kw):
            pass

        async def run(self, prompt, **kw):
            llamadas.append(1)
            return _Salida("{}")

    monkeypatch.setattr(experts, "Agent", _Agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: object())

    import time as _t
    r, fallo, _ = await planificador._pedir_plan(
        ["a:1", "b:2"], 0, "dame el plan", experts,
        limite=_t.monotonic() - 1)          # presupuesto ya vencido

    assert r is None and not llamadas, "gasto una llamada sin presupuesto"
    assert isinstance(fallo, TimeoutError)


async def test_reintentos_recalculan_el_presupuesto_restante(monkeypatch):
    """El backoff y el intento previo también consumen el tope total."""
    from relay import experts
    import asyncio

    reloj = [100.0]
    timeouts = []
    intentos = [0]

    class Http500(Exception):
        status_code = 500

    class Agente:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            intentos[0] += 1
            reloj[0] += 3
            if intentos[0] == 1:
                raise Http500()
            return _Salida(_BUENO)

    async def wait_for(coro, timeout):
        timeouts.append(timeout)
        return await coro

    async def sleep(seconds):
        reloj[0] += seconds

    monkeypatch.setattr(experts, "Agent", Agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: spec)
    monkeypatch.setattr(planificador.time, "monotonic", lambda: reloj[0])
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(planificador, "_BACKOFF_S", (2.0, 2.0))

    r, fallo, _ = await planificador._pedir_plan(
        ["modelo"], 0, "plan", experts, limite=110.0)

    assert r is not None and fallo is None
    assert timeouts == [10.0, 5.0]


async def test_el_backoff_no_sobrepasa_el_tope_total(monkeypatch):
    from relay import experts
    import asyncio

    reloj = [100.0]
    esperas = []

    class Http500(Exception):
        status_code = 500

    class Agente:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            reloj[0] += 8
            raise Http500()

    async def wait_for(coro, timeout):
        return await coro

    async def sleep(seconds):
        esperas.append(seconds)
        reloj[0] += seconds

    monkeypatch.setattr(experts, "Agent", Agente)
    monkeypatch.setattr(experts, "build_model", lambda spec: spec)
    monkeypatch.setattr(planificador.time, "monotonic", lambda: reloj[0])
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(planificador, "_BACKOFF_S", (20.0, 20.0))

    r, fallo, _ = await planificador._pedir_plan(
        ["modelo"], 0, "plan", experts, limite=110.0)

    assert r is None and isinstance(fallo, TimeoutError)
    assert esperas == []
