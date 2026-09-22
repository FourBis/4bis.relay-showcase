"""F2: correr el grafo con dos nodos en paralelo sin pisarse (2026-08-17).

Pedido del usuario: *"2 nodos en paralelo, y que los archivos en paralelo
no toquen otro archivo que ya está tocando otro bot"*.

Eso se resuelve en dos capas y las dos se prueban acá:

  1. `grafo.elegibles` no co-agenda dos tareas cuyos archivos declarados
     se pisan → evita el choque.
  2. `files.Permisos.reservadas` rechaza la escritura de un archivo
     tomado por otra tarea viva → lo impide. Hace falta porque la
     declaración la escribe un LLM y puede quedarse corta: descubre a
     mitad del trabajo que también hay que tocar otro archivo.

El loop recibe `ejecutar` inyectado, así que todo esto corre sin modelo.
"""
from __future__ import annotations
from relay import experts, expert_models, expert_runner, expert_selection, progress, orchestrator_execution

import asyncio
import json
from pathlib import Path

import pytest

from relay import files, grafo, orquestador
from relay.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    await d.init_schema()
    return d


def _n(id_, *deps, archivos=(), **kw):
    return grafo.Nodo(id=id_, titulo=id_, deps=tuple(deps),
                      archivos=tuple(archivos), **kw)


# ---------- 1. la capa que EVITA el choque ----------


def test_dos_tareas_que_tocan_el_mismo_archivo_no_van_juntas():
    nodos = [_n("a", archivos=["src/db.py"]), _n("b", archivos=["src/db.py"])]
    elegidos = grafo.elegibles(nodos, tope=2)
    assert [x.id for x in elegidos] == ["a"]


def test_dos_escritores_no_van_juntos_aunque_declaren_archivos_distintos():
    """Desde el 9/9/2026 se serializa a todo el que declare `archivos`.

    Antes iban juntos si las listas no se pisaban. Esa precision era
    falsa: la reserva por archivo la respetan las tools de archivo, pero
    NO la `shell`, que corre un comando arbitrario y puede escribir
    cualquier cosa. Con dos escritores vivos, la exclusion dependia de
    que el agente se portara bien.
    """
    nodos = [_n("a", archivos=["src/db.py"]), _n("b", archivos=["src/ui.py"])]
    assert [x.id for x in grafo.elegibles(nodos, tope=2)] == ["a"]


def test_un_escritor_y_un_lector_si_van_juntos():
    """El contrapeso: serializar TODO perderia el punto del DAG.

    Un nodo sin `archivos` es el que solo lee o corre comandos —el de
    verificacion, que corre el build— y sigue yendo en paralelo.
    """
    nodos = [_n("a", archivos=["src/db.py"]), _n("b")]
    assert [x.id for x in grafo.elegibles(nodos, tope=2)] == ["a", "b"]


def test_el_tope_manda_aunque_no_haya_conflicto():
    nodos = [_n("a"), _n("b"), _n("c")]
    assert len(grafo.elegibles(nodos, tope=2)) == 2


def test_una_carpeta_declarada_choca_con_un_archivo_de_adentro():
    """Si no, 'toco todo src/relay' correría junto a 'toco src/relay/db.py'."""
    nodos = [_n("a", archivos=["src/relay/"]),
             _n("b", archivos=["src/relay/db.py"])]
    assert [x.id for x in grafo.elegibles(nodos, tope=2)] == ["a"]


def test_las_mayusculas_no_dejan_pasar_dos_sobre_el_mismo_archivo():
    """El caso real es Windows: Src/Main.py y src/main.py son el mismo."""
    nodos = [_n("a", archivos=["Src/Main.py"]), _n("b", archivos=["src/main.py"])]
    assert [x.id for x in grafo.elegibles(nodos, tope=2)] == ["a"]


def test_sin_archivos_declarados_no_conflictuan():
    """Un nodo que solo lee o corre comandos no debe serializar el grafo."""
    nodos = [_n("a"), _n("b")]
    assert len(grafo.elegibles(nodos, tope=2)) == 2


def test_no_arranca_algo_que_pisa_a_lo_que_ya_esta_corriendo():
    nodos = [_n("a", estado=grafo.CORRIENDO, archivos=["src/db.py"]),
             _n("b", archivos=["src/db.py"])]
    corriendo = [n for n in nodos if n.estado == grafo.CORRIENDO]
    assert grafo.elegibles(nodos, tope=2, en_curso=corriendo) == []


def test_el_orden_no_se_reordena_para_meter_mas():
    """Si se reordenara, el mismo grafo avanzaría distinto cada vez y con
    un humano mirando la UI eso se ve como que hace lo que quiere."""
    # `c` no declara archivos, asi que no choca con `a`: entra sin que
    # haya que reordenar nada. Con `c` escribiendo tambien, la respuesta
    # correcta hoy seria solo `["a"]`.
    nodos = [_n("a", archivos=["x.py"], orden=0),
             _n("b", archivos=["x.py"], orden=1),
             _n("c", orden=2)]
    assert [x.id for x in grafo.elegibles(nodos, tope=2)] == ["a", "c"]


# ---------- 2. la capa que lo IMPIDE ----------


async def test_una_reserva_bloquea_la_escritura(db, tmp_path):
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "uno", "archivos": ["src/db.py"]},
        {"id": "t2", "titulo": "dos"}])
    await db.claim_task_files("t1", "g", ["src/db.py"])

    reservadas = await db.files_claimed_by_others("t2")
    perm = files.Permisos.para(str(tmp_path), reservadas=reservadas)
    (tmp_path / "src").mkdir()
    with pytest.raises(files.SinPermiso) as e:
        files.escribir(perm, "src/db.py", "x")
    assert "otra tarea" in str(e.value)


async def test_el_dueño_de_la_reserva_si_escribe(db, tmp_path):
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "uno"}])
    await db.claim_task_files("t1", "g", ["src/db.py"])
    perm = files.Permisos.para(
        str(tmp_path), reservadas=await db.files_claimed_by_others("t1"))
    files.escribir(perm, "src/db.py", "contenido")
    assert (tmp_path / "src" / "db.py").read_text() == "contenido"


async def test_leer_lo_reservado_sigue_permitido(db, tmp_path):
    """Prohibir la lectura serializaría el grafo sin ganar nada."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "db.py").write_text("hola")
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "uno"},
                                                 {"id": "t2", "titulo": "dos"}])
    await db.claim_task_files("t1", "g", ["src/db.py"])
    perm = files.Permisos.para(
        str(tmp_path), reservadas=await db.files_claimed_by_others("t2"))
    assert "hola" in files.leer(perm, "src/db.py")


async def test_editar_y_mover_tambien_respetan_la_reserva(db, tmp_path):
    (tmp_path / "a.txt").write_text("uno")
    (tmp_path / "b.txt").write_text("dos")
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "uno"},
                                                 {"id": "t2", "titulo": "dos"}])
    await db.claim_task_files("t1", "g", ["a.txt"])
    perm = files.Permisos.para(
        str(tmp_path), reservadas=await db.files_claimed_by_others("t2"))
    with pytest.raises(files.SinPermiso):
        files.editar(perm, "a.txt", "uno", "otro")
    with pytest.raises(files.SinPermiso):
        files.mover(perm, "b.txt", "a.txt")     # el DESTINO también cuenta


async def test_no_se_puede_tomar_lo_que_otro_tiene(db):
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"},
                                                 {"id": "t2", "titulo": "2"}])
    await db.claim_task_files("t1", "g", ["src/db.py"])
    rechazados = await db.claim_task_files("t2", "g", ["src/db.py"])
    assert rechazados == ["src/db.py"]


async def test_las_reservas_de_tareas_muertas_se_sueltan(db):
    """Un run que muere sin soltar dejaría el archivo tomado para siempre
    y el grafo se vería colgado sin que nadie pueda decir por qué."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    await db.claim_task_files("t1", "g", ["src/db.py"])
    await db.update_task("t1", estado=grafo.FALLADO)   # murió sin soltar
    assert await db.release_dead_claims("g") == 1
    assert await db.files_claimed_by_others("t2") == set()


# ---------- 3. el loop entero ----------


async def test_corre_el_grafo_de_punta_a_punta(db):
    await db.create_task_graph("g", "objetivo", tareas=[
        {"id": "t1", "titulo": "leer", "idempotente": True},
        {"id": "t2", "titulo": "a", "deps": ["t1"], "archivos": ["a.py"]},
        {"id": "t3", "titulo": "b", "deps": ["t1"], "archivos": ["b.py"]},
        {"id": "t4", "titulo": "cerrar", "deps": ["t2", "t3"]}])
    corridas = []

    async def ejecutar(tarea):
        corridas.append(tarea["id"])
        return {"ok": True, "resultado": f"hecho {tarea['id']}",
                "modelo": "minimax:MiniMax-M3"}

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    assert prog["estado"] == "hecho"
    assert prog["hechos"] == 4
    assert corridas[0] == "t1" and corridas[-1] == "t4"
    g = await db.get_task_graph("g")
    assert (await db.get_task_graph("g"))["estado"] == "hecho"
    assert all(t["modelo"] == "minimax:MiniMax-M3" for t in g["tasks"])


async def test_nunca_hay_dos_corriendo_sobre_el_mismo_archivo(db):
    """La prueba que le importa al usuario, sobre el loop real."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": f"t{i}", "titulo": str(i), "archivos": ["compartido.py"]}
        for i in range(4)])
    vivos: set = set()
    solapes = []

    async def ejecutar(tarea):
        vivos.add(tarea["id"])
        if len(vivos) > 1:
            solapes.append(sorted(vivos))
        await asyncio.sleep(0.02)
        vivos.discard(tarea["id"])
        return {"ok": True, "resultado": "ok"}

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar, tope=2)
    assert prog["hechos"] == 4
    assert solapes == [], f"dos bots sobre el mismo archivo: {solapes}"


async def test_dos_en_paralelo_cuando_no_se_pisan(db):
    """El contrapeso del test de arriba: sin conflicto, sí van juntas.

    Los nodos NO declaran `archivos`: desde el 9/9/2026 dos escritores
    se serializan siempre, asi que el paralelismo que queda —y el que
    importa— es el de los que leen o corren comandos.
    """
    await db.create_task_graph("g", "x", tareas=[
        {"id": f"t{i}", "titulo": str(i)} for i in range(4)])
    maximo = {"n": 0}
    vivos: set = set()

    async def ejecutar(tarea):
        vivos.add(tarea["id"])
        # Esperar a que aparezca la compañera en vez de dormir un rato
        # fijo. Entre un lanzamiento y el siguiente el orquestador hace
        # dos idas a la base (`update_task` + `claim_task_files`): medido
        # en Windows son ~16 ms, así que con el `sleep(0.02)` que había
        # acá la primera tarea terminaba antes de que arrancara la
        # segunda y el test fallaba por velocidad de disco, no porque el
        # motor serializara. Si de verdad serializa, esto NO cuelga: cada
        # tarea espera su tope y `maximo` se queda en 1.
        for _ in range(200):
            if len(vivos) >= 2:
                break
            await asyncio.sleep(0.005)
        maximo["n"] = max(maximo["n"], len(vivos))
        vivos.discard(tarea["id"])
        return {"ok": True, "resultado": "ok"}

    await orquestador.correr_grafo(db, "g", ejecutar=ejecutar, tope=2)
    assert maximo["n"] == 2


async def test_un_fallo_bloquea_a_los_suyos_y_no_al_resto(db):
    # `malo` es idempotente con 1 intento: falla y NO puede reintentar,
    # así que llega a `fallado` sin pasar por la pregunta. Es el camino
    # que ejercita el bloqueo en cascada.
    await db.create_task_graph("g", "x", tareas=[
        {"id": "malo", "titulo": "malo", "idempotente": True,
         "max_intentos": 1},
        {"id": "hijo", "titulo": "hijo", "deps": ["malo"]},
        {"id": "otro", "titulo": "otro"}])

    async def ejecutar(tarea):
        if tarea["id"] == "malo":
            return {"ok": False, "error": "permiso denegado"}
        return {"ok": True, "resultado": "ok"}

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    por_id = {t["id"]: t for t in (await db.get_task_graph("g"))["tasks"]}
    assert por_id["malo"]["estado"] == grafo.FALLADO
    assert por_id["hijo"]["estado"] == grafo.BLOQUEADO
    assert por_id["otro"]["estado"] == grafo.HECHO   # la rama sana siguió
    assert prog["estado"] == "fallado"


async def test_un_nodo_idempotente_se_reintenta_solo(db):
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "leer", "idempotente": True, "max_intentos": 3}])
    intentos = {"n": 0}

    async def ejecutar(tarea):
        intentos["n"] += 1
        if intentos["n"] < 3:
            return {"ok": False, "error": "timeout"}
        return {"ok": True, "resultado": "a la tercera"}

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    assert intentos["n"] == 3
    assert prog["estado"] == "hecho"


async def test_un_nodo_que_escribe_no_se_reintenta_solo(db):
    """La regla que acordamos: reintentar algo que ya escribió puede
    duplicar el efecto, y el runtime no sabe si ya ocurrió."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "migrar", "archivos": ["schema.sql"]}])
    intentos = {"n": 0}

    async def ejecutar(tarea):
        intentos["n"] += 1
        return {"ok": False, "error": "a mitad de camino"}

    await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    assert intentos["n"] == 1
    por_id = {t["id"]: t for t in (await db.get_task_graph("g"))["tasks"]}
    assert por_id["t1"]["estado"] == grafo.ESPERANDO


async def test_el_coordinador_entra_solo_cuando_algo_falla(db):
    """Verificar cada nodo con el modelo caro dobla el costo del grafo
    para revisar tareas que salieron bien."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "ok1", "titulo": "va bien"},
        {"id": "mal", "titulo": "va mal"}])
    llamadas = []

    async def ejecutar(tarea):
        if tarea["id"] == "mal":
            return {"ok": False, "error": "boom"}
        return {"ok": True, "resultado": "ok"}

    async def coordinar(tarea, res):
        llamadas.append(tarea["id"])
        return "fallar"

    await orquestador.correr_grafo(db, "g", ejecutar=ejecutar,
                                   coordinar=coordinar)
    assert llamadas == ["mal"]


async def test_el_coordinador_puede_pedir_un_reintento_que_la_regla_negaria(db):
    """Nemotron mira el error y decide: 'esto fue un 429, reintentá'."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "escribe", "max_intentos": 5}])
    intentos = {"n": 0}

    async def ejecutar(tarea):
        intentos["n"] += 1
        return {"ok": intentos["n"] >= 2, "error": "429", "resultado": "ok"}

    async def coordinar(tarea, res):
        return "reintentar"

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar,
                                          coordinar=coordinar)
    assert intentos["n"] == 2 and prog["estado"] == "hecho"


async def test_un_nodo_que_revienta_no_voltea_el_grafo(db):
    await db.create_task_graph("g", "x", tareas=[
        {"id": "boom", "titulo": "boom"}, {"id": "sano", "titulo": "sano"}])

    async def ejecutar(tarea):
        if tarea["id"] == "boom":
            raise RuntimeError("explotó")
        return {"ok": True, "resultado": "ok"}

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    por_id = {t["id"]: t for t in (await db.get_task_graph("g"))["tasks"]}
    # Una excepción es un fallo como cualquier otro: se le aplica la
    # misma regla (no es idempotente → para y pregunta), pero el error
    # queda registrado y el resto del grafo sigue.
    assert por_id["boom"]["estado"] == grafo.ESPERANDO
    assert "RuntimeError" in por_id["boom"]["error"]
    assert por_id["sano"]["estado"] == grafo.HECHO


async def test_una_tarea_que_pregunta_no_se_reintenta(db):
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "instalar", "idempotente": True}])
    intentos = {"n": 0}

    async def ejecutar(tarea):
        intentos["n"] += 1
        return {"ok": False, "pregunta": "q_123", "resultado": "¿instalo?"}

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    assert intentos["n"] == 1
    por_id = {t["id"]: t for t in (await db.get_task_graph("g"))["tasks"]}
    assert por_id["t1"]["estado"] == grafo.ESPERANDO
    assert prog["estado"] == "activo"     # en pausa, no muerto


async def test_los_archivos_se_sueltan_pase_lo_que_pase(db):
    """Una tarea que falla y se queda con el archivo traba a todas las
    que lo necesitan, y el grafo se ve colgado sin motivo visible."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "falla", "archivos": ["x.py"]}])

    async def ejecutar(tarea):
        return {"ok": False, "error": "boom"}

    await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    assert await db.files_claimed_by_others("otra") == set()


async def test_el_callback_de_cambio_no_puede_voltear_el_grafo(db):
    """La UI se entera por acá; que la UI falle no puede parar el trabajo."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])

    async def ejecutar(tarea):
        return {"ok": True, "resultado": "ok"}

    async def on_cambio(g):
        raise RuntimeError("la UI explotó")

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar,
                                          on_cambio=on_cambio)
    assert prog["estado"] == "hecho"


# ---------- 4. "para y pregunta" tiene que preguntar de verdad ----------


async def test_un_nodo_que_para_deja_la_decision_anotada(db):
    """El agujero que tenía la primera versión: marcaba el estado y no
    preguntaba nada. Un grafo esperando a un humano que nunca se enteró
    de que lo esperaban es peor que uno que falla."""
    await db.create_task_graph("g", "objetivo", tareas=[
        {"id": "t1", "titulo": "migrar el esquema", "archivos": ["schema.sql"]}],
        conversation_id="conv-1", project_slug="demo")

    async def ejecutar(tarea):
        return {"ok": False, "error": "la migración quedó a mitad"}

    await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)

    abiertas = await db.list_expert_questions(conversation_id="conv-1")
    assert len(abiertas) == 1
    import json
    q = json.loads(abiertas[0]["question_json"])
    assert "migrar el esquema" in q["title"]
    assert "a mitad" in q["detail"]
    # Las tres salidas reales que tiene el humano, no un sí/no.
    assert [o["label"] for o in q["options"]] == [
        "Reintentala igual", "Dala por fallada y seguí con el resto",
        "Pará el plan"]


async def test_sin_conversacion_no_explota_al_preguntar(db):
    """Un grafo suelto (sin hilo) igual tiene que terminar su vuelta."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])

    async def ejecutar(tarea):
        return {"ok": False, "error": "boom"}

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    assert prog["esperando_humano"] == 1


async def test_max_intentos_del_planificador_se_respeta(db):
    """Bug encontrado por el test del reintento: `max_intentos` venía en
    el dict de la tarea y se ignoraba en silencio — el nodo quedaba con
    el default 2 dijera lo que dijera el planificador."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "1", "max_intentos": 5}])
    fila = (await db.get_task_graph("g"))["tasks"][0]
    assert fila["max_intentos"] == 5


# ---------- 5. el cableado real (MiniMax ejecuta, nemotron coordina) ----------
#
# Acá se prueba lo único del módulo que habla con pydantic-ai, con
# `run_expert` y `Agent` reemplazados: lo que importa es CÓMO se los
# llama, no qué contestan.

from unittest.mock import patch


def _proyecto(tmp_path):
    return {"slug": "demo", "repo_path": str(tmp_path), "system_prompt": "p",
            "mcp_servers": [], "native_tools": [], "defaults_json": {}}


async def test_el_nodo_corre_sin_las_etapas_y_sin_historial(db, tmp_path):
    """Lo que hace que un nodo sea corto — y de eso depende todo lo demás:
    el plan ya existe (es el grafo) y el hilo entero no es su insumo."""
    await db.create_task_graph("g", "objetivo", tareas=[
        {"id": "t1", "titulo": "leer el esquema"}], conversation_id="conv-1")
    g = await db.get_task_graph("g")
    capturado = {}

    async def fake_run_expert(proj, user, **kw):
        capturado.update(kw)
        capturado["prompt"] = user
        return {"content": "leí 12 tablas", "phase_at_end": "ok",
                "model": "minimax:MiniMax-M3"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(
            db, _proyecto(tmp_path), g, modelo="minimax:MiniMax-M3")
        res = await ejecutar(g["tasks"][0])

    # `run_expert` (no `run_expert_staged`) YA es el de un solo turno:
    # sin planificador, verificador ni documentador. Eso es lo que hace
    # que un nodo sea corto.
    assert not capturado.get("message_history_json")
    assert capturado["model_override"] == "minimax:MiniMax-M3"
    assert res["ok"] is True
    assert res["resultado"] == "leí 12 tablas"


async def test_el_prompt_del_nodo_lleva_lo_que_dejaron_sus_dependencias(db, tmp_path):
    """El nodo que carga datos necesita saber qué esquema dejó el que migró."""
    await db.create_task_graph("g", "objetivo", tareas=[
        {"id": "t1", "titulo": "migrar"},
        {"id": "t2", "titulo": "cargar", "deps": ["t1"]}])
    await db.update_task("t1", estado=grafo.HECHO,
                         resultado="agregué la columna `estado`")
    g = await db.get_task_graph("g")
    t2 = next(t for t in g["tasks"] if t["id"] == "t2")
    prompt = orquestador._prompt_de_tarea(g, t2, g["tasks"])

    assert "cargar" in prompt
    assert "agregué la columna `estado`" in prompt
    assert "Objetivo general" in prompt
    # …y le dice que NO siga con las que vienen después.
    assert "no sigas" in prompt.lower()


async def test_el_prompt_no_lleva_las_tareas_ajenas(db, tmp_path):
    """Un nodo que ve doce tareas ajenas empieza a opinar sobre ellas."""
    await db.create_task_graph("g", "objetivo", tareas=[
        {"id": "t1", "titulo": "la mía"},
        {"id": "otra", "titulo": "TAREA AJENA QUE NO ME INCUMBE"}])
    g = await db.get_task_graph("g")
    t1 = next(t for t in g["tasks"] if t["id"] == "t1")
    assert "AJENA" not in orquestador._prompt_de_tarea(g, t1, g["tasks"])


async def test_los_archivos_reservados_llegan_al_run(db, tmp_path):
    """El cable que impide que dos bots se pisen, de punta a punta."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "uno"}, {"id": "t2", "titulo": "dos"}])
    await db.claim_task_files("t1", "g", ["src/db.py"])
    g = await db.get_task_graph("g")
    capturado = {}

    async def fake_run_expert(proj, user, **kw):
        capturado.update(kw)
        return {"content": "ok", "phase_at_end": "ok"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        await ejecutar(next(t for t in g["tasks"] if t["id"] == "t2"))

    assert "src/db.py" in capturado["archivos_reservados"]


async def test_un_nodo_que_pregunta_se_reporta_como_pregunta(db, tmp_path):
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "¿instalo pwsh?", "phase_at_end": "ok",
                "question_id": "q_abc12345"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(g["tasks"][0])

    assert res["pregunta"] == "q_abc12345"
    assert res["ok"] is False       # preguntar no es terminar


async def test_un_nodo_que_pregunta_no_cierra_el_chat_mudo(db, tmp_path):
    """29 de 81 chats en `error` no decían por qué (barrido del 4/9): la
    columna `error` NULL o vacía, todos `source='grafo'` y
    `phase_at_end='writing'`. Eran nodos que habían abierto una pregunta
    al humano: no están rotos —`error` quedaba ""— pero `ok` se va a
    False por `question_id`, así que se guardaban sin decir por qué.
    Ese primer fix (4/9) sólo puso el mensaje; el status seguía siendo
    `error` (ver `test_un_nodo_que_pregunta_cierra_el_chat_ok_no_error`,
    que corrige eso el 6/9)."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "¿instalo pwsh?", "phase_at_end": "writing",
                "question_id": "q_abc12345"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(g["tasks"][0])

    assert res["ok"] is False
    assert "q_abc12345" in res["error"]
    fila = await db.get_chat(res["chat_id"])
    assert (fila["error"] or "").strip(), "el chat quedó cerrado sin motivo"


async def test_un_nodo_que_pregunta_cierra_el_chat_ok_no_error(db, tmp_path):
    """El status con que se cierra el chat depende SOLO de si el nodo
    está roto de verdad (`fase in FASES_INCOMPLETAS`), no de
    `salida["ok"]`. Esa bandera mezcla dos preguntas distintas: "¿el
    grafo sigue avanzando?" (correctamente False cuando hay pregunta,
    para que la tarea no quede `hecho`) y "¿esto salió bien?" (debería
    ser sí: preguntar no es fallar).

    Medido sobre relay.db (barrido del 6/9), separación perfecta por
    camino: la UI preguntó 79 veces y cerró 79 `ok`, 0 `error`; la API
    preguntó 1 vez y cerró `ok`; el grafo preguntó 32 veces y cerró las
    32 como `error`. Misma tool (`ask_human`), mismo comportamiento,
    etiqueta opuesta según qué código cierra el chat — el camino de la
    UI es la referencia correcta.
    """
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "¿instalo pwsh?", "phase_at_end": "writing",
                "question_id": "q_abc12345"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(g["tasks"][0])

    assert res["ok"] is False          # el grafo sigue esperando: correcto
    fila = await db.get_chat(res["chat_id"])
    assert fila["status"] == "ok", "una pregunta al humano no es un error"


async def test_un_nodo_realmente_roto_sigue_cerrando_error(db, tmp_path):
    """La otra cara del mismo cambio: un nodo que sí está roto (fase en
    `FASES_INCOMPLETAS`, sin pregunta de por medio) tiene que seguir
    cerrando `error`. El fix de status depende de `roto`, no puede
    convertirse en un blanqueo general de todos los cierres."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "a medias", "phase_at_end": "idle_timeout"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(g["tasks"][0])

    assert res["ok"] is False
    fila = await db.get_chat(res["chat_id"])
    assert fila["status"] == "error"


async def test_ningun_cierre_no_ok_puede_quedar_sin_motivo(db):
    """La red de raíz: `_cerrar_chat` es el único punto por el que pasan
    todos los cierres del grafo, así que el guard va ahí y no por rama."""
    chat_id = await db.create_chat(project_slug="demo", source="grafo",
                                   author="orquestador", target="demo",
                                   user_prompt="x")
    await orquestador._cerrar_chat(db, chat_id, "error", "")
    fila = await db.get_chat(chat_id)
    assert (fila["error"] or "").strip(), "cierre mudo: la columna quedó NULL"
    assert "sin motivo declarado" in fila["error"]


async def test_un_nodo_que_corta_por_reloj_es_un_fallo(db, tmp_path):
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "a medias", "phase_at_end": "idle_timeout"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(g["tasks"][0])

    assert res["ok"] is False
    assert "idle_timeout" in res["error"]


@pytest.mark.parametrize("fase", ["budget_exceeded", "provider_error",
                                  "off_plan"])
async def test_un_nodo_cortado_a_mitad_no_queda_hecho(db, tmp_path, fase):
    """Quedarse sin presupuesto, que se caiga el proveedor o desviarse
    del plan NO es haber terminado. Estas tres fases faltaban en la lista
    del orquestador —que sí las tenía el panel— y el nodo se marcaba
    `hecho`.

    Medido en code-hero-rpg (31/8): de siete tareas, `Corregir bugs
    críticos de jugabilidad` murió en `provider_error` y `Corregir bugs
    de estabilidad` en `budget_exceeded` tras 255 tool calls. El grafo
    cerró 7/7 y el humano preguntó "¿queda algo pendiente?" — le faltaba
    justo lo que esas dos tareas tenían que arreglar.
    """
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "a medias", "phase_at_end": fase}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(g["tasks"][0])

    assert res["ok"] is False, f"{fase} no puede contar como terminado"
    assert fase in res["error"]


def test_el_panel_y_el_grafo_usan_la_misma_lista_de_fases():
    """El bug no era la lista sino que hubiera DOS. Si alguien vuelve a
    escribir una a mano, esto lo dice antes que un grafo mintiendo."""
    from relay import grafo as G
    from relay import server

    assert server._PHASE_FALLIDO is G.FASES_INCOMPLETAS


async def test_si_run_expert_revienta_el_nodo_falla_pero_el_grafo_no(db, tmp_path):
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")

    async def fake_run_expert(proj, user, **kw):
        raise RuntimeError("el proveedor se cayó")

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(g["tasks"][0])

    assert res["ok"] is False and "RuntimeError" in res["error"]


# ---------- el coordinador ----------


class _AgenteFalso:
    respuesta = "FALLAR"

    def __init__(self, *a, **kw):
        pass

    async def run(self, prompt, **kw):
        class R:
            output = _AgenteFalso.respuesta
        _AgenteFalso.ultimo_prompt = prompt
        return R()


@pytest.mark.parametrize("dice,espera", [
    ("REINTENTAR", "reintentar"), ("FALLAR", "fallar"),
    ("PREGUNTAR", "preguntar"),
    ("Creo que lo mejor es REINTENTAR.", "reintentar"),
])
async def test_el_coordinador_traduce_su_palabra(dice, espera, tmp_path):
    from relay import experts
    _AgenteFalso.respuesta = dice
    with patch.object(experts, "Agent", _AgenteFalso), \
         patch.object(experts, "build_model", lambda s: object()):
        coordinar = orquestador.coordinador_nemotron({"slug": "demo"})
        r = await coordinar({"id": "t1", "titulo": "x", "idempotente": 1,
                             "intentos": 1, "max_intentos": 3},
                            {"error": "429"})
    assert r == espera


async def test_el_coordinador_no_puede_reintentar_lo_que_no_es_idempotente(tmp_path):
    """El límite que fijó el humano no lo puede ablandar el modelo: no
    tiene forma de saber si el efecto ya ocurrió. Puede pedir PREGUNTAR,
    no reintentar a ciegas."""
    from relay import experts
    _AgenteFalso.respuesta = "REINTENTAR"
    with patch.object(experts, "Agent", _AgenteFalso), \
         patch.object(experts, "build_model", lambda s: object()):
        coordinar = orquestador.coordinador_nemotron({"slug": "demo"})
        r = await coordinar({"id": "t1", "titulo": "migrar", "idempotente": 0,
                             "intentos": 1, "max_intentos": 3},
                            {"error": "429"})
    assert r == "preguntar"


async def test_si_el_coordinador_no_contesta_manda_la_regla(tmp_path):
    """Sin coordinador el grafo tiene que seguir decidiendo igual."""
    from relay import experts

    def _explota(spec):
        raise RuntimeError("nemotron caído")

    with patch.object(experts, "build_model", _explota):
        coordinar = orquestador.coordinador_nemotron({"slug": "demo"})
        r = await coordinar({"id": "t1", "titulo": "x", "idempotente": 1,
                             "intentos": 1, "max_intentos": 3}, {"error": "x"})
    assert r is None      # None = decide `decidir_tras_fallo`


async def test_el_ejecutor_llama_a_run_expert_con_kwargs_que_existen(db, tmp_path):
    """El hueco que dejaron los mocks de arriba.

    Mockeando `run_expert` con `**kw` cualquier kwarg pasa, incluso uno
    que la función real no acepta — y así se me coló `three_stage=False`,
    que es de `run_expert_staged`. Lo destapó el smoke, no la suite. Acá
    se compara contra la firma REAL.
    """
    import inspect

    from relay import experts

    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")
    capturado = {}

    async def fake_run_expert(proj, user, **kw):
        capturado.update(kw)
        return {"content": "ok", "phase_at_end": "ok"}

    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        await ejecutar(g["tasks"][0])

    acepta = set(inspect.signature(expert_runner.run_expert).parameters)
    de_mas = set(capturado) - acepta
    assert not de_mas, f"run_expert no acepta: {sorted(de_mas)}"


async def test_el_chat_del_nodo_se_cierra(db, tmp_path):
    """Un chat en `running` para siempre ensucia 'En curso' y el barrido
    de zombies: cada nodo del grafo dejaría uno."""
    from relay import experts

    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "ok", "phase_at_end": "ok", "tokens_in": 10}

    with patch.object(experts, "run_expert", fake_run_expert):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(g["tasks"][0])

    chat = await db.get_chat(res["chat_id"])
    assert chat["status"] == "ok"
    assert chat["tokens_in"] == 10


# ---------- 6. lo que encontró la revisión del 2026-08-23 ----------
#
# Cuatro agujeros que la suite no veía porque nadie miraba el efecto,
# solo el estado: la pregunta se creaba pero colgada de un chat que no
# existía, las reservas de un grafo muerto bloqueaban a los que venían
# después, la capa que "garantiza" no veía el caso carpeta/archivo, y
# salir del loop por la mala dejaba nodos corriendo sueltos.


async def test_la_pregunta_queda_colgada_del_chat_del_nodo(db):
    """No alcanza con marcar `esperando_humano`: la tarjeta se busca por
    `chat_id`, así que con el chat equivocado el humano nunca la ve."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "migrar"}])

    async def ejecutar(tarea):
        return {"ok": False, "error": "se cayó", "chat_id": "chat_del_nodo"}

    await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)

    preguntas = await db.list_expert_questions(chat_id="chat_del_nodo")
    assert len(preguntas) == 1, "la pregunta no quedó en el chat del nodo"
    assert "migrar" in preguntas[0]["question_json"]
    # …y NO colgada del id del grafo, que no es un chat de nadie.
    assert not await db.list_expert_questions(chat_id="g")


async def test_una_reserva_de_otro_grafo_muerto_no_bloquea_para_siempre(db):
    """El bloqueo mira TODAS las reservas vivas; el barrido tiene que
    tener el mismo alcance o queda un archivo tomado por nadie."""
    await db.create_task_graph("gA", "x", tareas=[
        {"id": "a1", "titulo": "a", "archivos": ["src/x.py"]}])
    await db.create_task_graph("gB", "x", tareas=[
        {"id": "b1", "titulo": "b", "archivos": ["src/x.py"]}])
    await db.claim_task_files("a1", "gA", ["src/x.py"])
    await db.update_task("a1", estado=grafo.FALLADO)   # gA murió sin soltar

    await db.release_dead_claims("gB")                 # gB arranca y barre
    assert await db.files_claimed_by_others("b1") == set()


async def test_una_reserva_huerfana_de_un_grafo_borrado_tambien_se_barre(db):
    """`task_file_claims` no tiene FK, así que borrar el grafo no las
    cascadea: quedarían tomando archivos que ya no reclama nadie."""
    await db.create_task_graph("gA", "x", tareas=[
        {"id": "a1", "titulo": "a", "archivos": ["src/y.py"]}])
    await db.claim_task_files("a1", "gA", ["src/y.py"])
    await db.run("DELETE FROM task_graphs WHERE id=?", ("gA",))

    assert await db.release_dead_claims() == 1
    assert await db.files_claimed_by_others("otra") == set()


async def test_la_reserva_de_una_tarea_viva_no_se_barre(db):
    """El barrido global no puede llevarse puesto lo de un grafo que SÍ
    está corriendo ahora mismo."""
    await db.create_task_graph("gA", "x", tareas=[
        {"id": "a1", "titulo": "a", "archivos": ["src/z.py"]}])
    await db.claim_task_files("a1", "gA", ["src/z.py"])
    await db.update_task("a1", estado=grafo.CORRIENDO)

    assert await db.release_dead_claims("gB") == 0
    assert await db.files_claimed_by_others("b1") == {"src/z.py"}


async def test_la_capa_de_reservas_ve_el_caso_carpeta_archivo(db):
    """`grafo.elegibles` ya lo filtra, pero la capa que garantiza tiene
    que verlo sola: si no, lanza un nodo que después rebota al escribir."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "1"}, {"id": "t2", "titulo": "2"}])
    await db.claim_task_files("t1", "g", ["src/relay/db.py"])

    # t2 pide la carpeta entera: el archivo de t1 está adentro.
    assert await db.claim_task_files("t2", "g", ["src/relay/"]) == ["src/relay"]
    # y al revés: con la carpeta tomada, no se puede pedir un archivo de adentro
    await db.release_task_files("t1")
    await db.claim_task_files("t1", "g", ["src/relay/"])
    assert await db.claim_task_files("t2", "g", ["src/relay/db.py"]) \
        == ["src/relay/db.py"]


async def test_salir_por_la_mala_no_deja_nodos_corriendo_sueltos(db):
    """Cancelar el grafo (relay apagándose) tiene que cortar los runs y
    soltar sus archivos. Si no, siguen escribiendo y traban al que venga."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "larga", "archivos": ["src/a.py"],
         "idempotente": True}])
    arranco = asyncio.Event()
    murio = {"si": False}

    async def ejecutar(tarea):
        arranco.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            murio["si"] = True
            raise
        return {"ok": True}

    corrida = asyncio.create_task(
        orquestador.correr_grafo(db, "g", ejecutar=ejecutar))
    await asyncio.wait_for(arranco.wait(), timeout=5)
    corrida.cancel()
    with pytest.raises(asyncio.CancelledError):
        await corrida

    assert murio["si"], "el nodo siguió corriendo después de cancelar el grafo"
    assert await db.files_claimed_by_others("nadie") == set(), \
        "quedó un archivo tomado por una tarea que ya no existe"
    fila = (await db.get_task_graph("g"))["tasks"][0]
    # Idempotente → vuelve a `pendiente` y se puede retomar.
    assert fila["estado"] == grafo.PENDIENTE


@pytest.mark.parametrize("idempotente", [True, False])
async def test_cancelar_nodo_conserva_chat_historial_y_archivos(db, tmp_path, monkeypatch,
                                                             idempotente):
    from pydantic_ai import Tool
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.toolsets import FunctionToolset
    from relay import experts

    started = asyncio.Event()
    saved = tmp_path / "avance.txt"

    async def checkpoint() -> str:
        """Guarda un avance local y espera para simular cancelación en una tool."""
        saved.write_text("avance conservado", encoding="utf-8")
        started.set()
        await asyncio.Event().wait()
        return "terminado"

    def model(messages, info):
        return ModelResponse(parts=[ToolCallPart("checkpoint", {}, tool_call_id="step-1")])

    async def catalog(*args, **kwargs):
        return [FunctionToolset(tools=[Tool(checkpoint, takes_ctx=False)])], [], []

    monkeypatch.setattr(expert_models, "build_model", lambda spec: FunctionModel(model))
    monkeypatch.setattr(expert_selection, "_catalog_toolsets", catalog)
    await db.create_task_graph("g-cancel", "x", tareas=[{
        "id": "t1", "titulo": "Una", "idempotente": idempotente,
        "archivos": ["avance.txt"]}])
    graph = await db.get_task_graph("g-cancel")
    project = _proyecto(tmp_path)
    project["id"] = 1
    project["defaults_json"] = {"native_files": False, "native_shell": False,
                                "sql_tools": False, "timeout": 60}
    execute = orquestador.ejecutor_minimax(db, project, graph, modelo="fake")
    run = asyncio.create_task(orquestador.correr_grafo(db, "g-cancel", ejecutar=execute))
    try:
        await asyncio.wait_for(started.wait(), 3)
        before = (await db.get_task_graph("g-cancel"))["tasks"][0]
        chat_id = before["chat_id"]
        assert chat_id
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, 3)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    task = (await db.get_task_graph("g-cancel"))["tasks"][0]
    assert task["chat_id"] == chat_id
    assert task["estado"] == (grafo.PENDIENTE if idempotente else grafo.ESPERANDO)
    assert await db.files_claimed_by_others("nadie") == set()
    assert saved.read_text(encoding="utf-8") == "avance conservado"
    chat = await db.get_chat(chat_id)
    assert chat["status"] == chat["phase_at_end"] == "cancelled"
    assert chat["finished_at"] and chat["tool_calls"] == 1
    assert chat["tokens_in"] is not None and chat["duration_ms"] >= 0
    assert any(e.get("tool") == "checkpoint" for e in json.loads(chat["progress_events"]))
    output = (await db.run("SELECT payload, exported FROM chat_outputs WHERE chat_id=?",
                           (chat_id,)))[0]
    history = json.loads(output["payload"])["messages_json"]
    assert "checkpoint" in history and "step-1" in history
    assert output["exported"] == 1
    assert history not in Path(chat["md_path"]).read_text(encoding="utf-8")
    if not idempotente:
        questions = await db.list_expert_questions(chat_id=chat_id, only_open=True)
        assert len(questions) == 1


async def test_una_reserva_pisada_no_gasta_un_intento(db):
    """Un lanzamiento que no ocurrió no puede consumir reintentos: un
    grafo trabado por reservas se quedaría sin intentos sin ejecutar."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "1", "archivos": ["src/a.py"]}])
    # Otro grafo, vivo, tiene el archivo tomado y no lo suelta.
    await db.create_task_graph("otro", "x", tareas=[
        {"id": "ajena", "titulo": "ajena", "archivos": ["src/a.py"]}])
    await db.update_task("ajena", estado=grafo.CORRIENDO)
    await db.claim_task_files("ajena", "otro", ["src/a.py"])

    prog = await orquestador.correr_grafo(db, "g", ejecutar=_nunca)
    fila = (await db.get_task_graph("g"))["tasks"][0]
    assert fila["intentos"] == 0, "gastó un intento sin haber ejecutado nada"
    assert fila["estado"] == grafo.PENDIENTE
    assert prog["estado"] == "activo"


async def _nunca(tarea):
    raise AssertionError("no se tendría que haber lanzado")


# ---------- 5. la verificación de cierre: UNA por grafo ----------
#
# Los grafos corrían sin verificador: la tarea que más se puede desviar
# era la única sin nadie mirando. Acá se prueba el cableado del cierre —
# cuándo corre, cuándo NO, y que ni caído ni disconforme cambie lo que el
# grafo hizo. El criterio del verificador en sí es de `_run_verifier`
# (test_three_stage.py); acá el turno va inyectado, sin modelo.


def _veredicto(g: dict) -> dict:
    return json.loads(g["verificacion_json"] or "{}")


async def _ok(tarea):
    return {"ok": True, "resultado": f"hecho {tarea['id']}"}


def _verificador_que_dice(verdict, feedback="", llamadas=None, usage=None):
    async def verificar(*, user, plan, executor_result):
        if llamadas is not None:
            llamadas.append({"user": user, "plan": plan,
                             "executor_result": executor_result})
        return {"verdict": verdict, "feedback": feedback,
                "usage": usage if usage is not None else {
                    "tokens_in": 800, "tokens_out": 90},
                "error": "", "modelo": "nvidia:barato"}
    return verificar


async def test_verifica_una_sola_vez_cuando_el_grafo_cierra_hecho(db):
    await db.create_task_graph("g", "arregla el login", tareas=[
        {"id": "t1", "titulo": "leer"},
        {"id": "t2", "titulo": "escribir", "deps": ["t1"]}])
    llamadas: list = []

    prog = await orquestador.correr_grafo(
        db, "g", ejecutar=_ok,
        verificar=_verificador_que_dice("complete", "coincide", llamadas))

    assert prog["estado"] == "hecho"
    # UNA por grafo y no una por nodo: ahí está toda la diferencia de
    # costo con la decisión que este cambio NO revierte.
    assert len(llamadas) == 1
    # El objetivo del grafo es el pedido y los nodos son el plan.
    assert llamadas[0]["user"] == "arregla el login"
    assert "[t1] leer" in llamadas[0]["plan"]
    assert "depende de: t1" in llamadas[0]["plan"]
    assert "hecho t2" in llamadas[0]["executor_result"]["content"]

    v = _veredicto(await db.get_task_graph("g"))
    assert v["verdict"] == "complete"
    assert v["feedback"] == "coincide"
    assert v["modelo"] == "nvidia:barato"
    # El costo de la etapa queda medido: "cuánto cuesta esto" se contesta
    # con lo guardado, sin instrumentar de nuevo.
    assert (v["tokens_in"], v["tokens_out"]) == (800, 90)


async def test_tambien_verifica_cuando_el_grafo_falla(db):
    """Un grafo que falla es justo donde más importa saber qué quedó."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "malo", "titulo": "malo", "idempotente": True,
         "max_intentos": 1}])

    async def ejecutar(tarea):
        return {"ok": False, "error": "permiso denegado"}

    llamadas: list = []
    prog = await orquestador.correr_grafo(
        db, "g", ejecutar=ejecutar,
        verificar=_verificador_que_dice("needs_human", "revisa esto a mano", llamadas))

    assert prog["estado"] == "fallado"
    assert len(llamadas) == 1
    assert llamadas[0]["executor_result"]["phase_at_end"] == "fallado"
    assert "permiso denegado" in llamadas[0]["executor_result"]["content"]
    assert _veredicto(await db.get_task_graph("g"))["verdict"] == "needs_human"


async def test_un_grafo_cancelado_no_paga_el_turno_de_verificacion(db):
    """Si un humano lo paró, no le cobramos un turno para decirle lo que
    ya sabe."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "1", "idempotente": True}])
    arranco = asyncio.Event()
    llamadas: list = []

    async def ejecutar(tarea):
        arranco.set()
        await asyncio.sleep(30)
        return {"ok": True}

    corrida = asyncio.create_task(orquestador.correr_grafo(
        db, "g", ejecutar=ejecutar,
        verificar=_verificador_que_dice("complete", llamadas=llamadas)))
    await asyncio.wait_for(arranco.wait(), timeout=5)
    corrida.cancel()
    with pytest.raises(asyncio.CancelledError):
        await corrida

    assert llamadas == [], "verificó un grafo que un humano había parado"
    assert (await db.get_task_graph("g"))["verificacion_json"] is None


async def test_un_grafo_ya_marcado_cancelado_tampoco_se_verifica(db):
    """El otro camino: lo cancelaron desde afuera mientras cerraba."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    await db.update_task("t1", estado=grafo.HECHO)
    await db.set_task_graph_state("g", "cancelado")
    llamadas: list = []

    await orquestador.correr_grafo(
        db, "g", ejecutar=_nunca,
        verificar=_verificador_que_dice("complete", llamadas=llamadas))

    assert llamadas == []


async def test_un_verificador_que_revienta_no_voltea_el_grafo(db):
    """El grafo ya terminó su trabajo: que falle la etapa de verificación
    es una falla de telemetría, no del trabajo."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])

    async def revienta(*, user, plan, executor_result):
        raise RuntimeError("el endpoint gratis tiró 429")

    prog = await orquestador.correr_grafo(db, "g", ejecutar=_ok,
                                          verificar=revienta)

    assert prog["estado"] == "hecho"
    g = await db.get_task_graph("g")
    assert g["estado"] == "hecho", "una etapa de telemetría volteó el grafo"
    assert [t["estado"] for t in g["tasks"]] == [grafo.HECHO]
    v = _veredicto(g)
    assert v["verdict"] == "", "aprobó un grafo que nadie llegó a mirar"
    assert "RuntimeError" in v["error"], "no quedó registrado que falló"


async def test_needs_more_no_relanza_el_grafo(db):
    """Mismo criterio que los grafos a medias en el boot: reparar es una
    cosa y arrancar trabajo que nadie pidió es otra. El humano decide,
    con el veredicto a la vista."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    corridas: list = []

    async def ejecutar(tarea):
        corridas.append(tarea["id"])
        return {"ok": True, "resultado": "listo"}

    prog = await orquestador.correr_grafo(
        db, "g", ejecutar=ejecutar,
        verificar=_verificador_que_dice("needs_more", "falta el test"))

    assert corridas == ["t1"], "el veredicto relanzó trabajo por su cuenta"
    assert prog["estado"] == "hecho"
    g = await db.get_task_graph("g")
    assert g["estado"] == "hecho"
    assert [t["estado"] for t in g["tasks"]] == [grafo.HECHO]
    assert _veredicto(g)["verdict"] == "needs_more"


def test_el_resumen_recorta_pero_nombra_lo_que_deja_afuera():
    """Un nodo omitido en silencio hace que el verificador juzgue "faltó
    la mitad del plan" sobre un recorte NUESTRO."""
    g = {"tasks": [{"id": f"t{i}", "orden": i, "titulo": f"tarea {i}",
                    "estado": "hecho", "resultado": "x" * 900, "error": "",
                    "deps": []} for i in range(40)]}
    txt = orquestador._resumen_de_nodos(g)

    assert len(txt) < orquestador.TOPE_RESUMEN_CHARS + 1200
    assert "t0 [hecho]" not in txt, "el primero tiene que ir con detalle"
    assert "t39 [hecho]" in txt, "los que no entran igual se nombran"
    assert "x" * (orquestador.TOPE_POR_NODO_CHARS + 1) not in txt


# ---------- 7. F4: retomar un grafo cortado (2026-08-23) ----------
#
# Dos mitades, y la segunda es la que faltaba de verdad: sanar lo que
# quedó `corriendo` cuando el proceso se cortó, y —la que hace que "para
# y pregunta" no sea un callejón sin salida— llevar la respuesta del
# humano a la tarea.


async def test_sanar_devuelve_a_la_vida_una_tarea_idempotente(db):
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "correr los tests", "idempotente": True,
         "archivos": ["src/a.py"]}])
    # Como la dejaría un relay que se murió a mitad de camino.
    await db.update_task("t1", estado=grafo.CORRIENDO)
    await db.claim_task_files("t1", "g", ["src/a.py"])

    assert await orquestador.sanar(db, "g") == 1
    fila = (await db.get_task_graph("g"))["tasks"][0]
    assert fila["estado"] == grafo.PENDIENTE
    assert "cortó" in fila["error"]
    # …y sobre todo: soltó el archivo. El barrido de reservas muertas NO
    # las toca mientras la tarea diga estar `corriendo`, así que sin
    # sanar quedaban tomadas para siempre.
    assert await db.files_claimed_by_others("otra") == set()


async def test_sanar_una_no_idempotente_para_y_pregunta(db):
    """Nadie sabe cuánto alcanzó a hacer antes del corte: repetirla
    podría duplicarlo."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "abrir el PR", "chat_id": "c1"}])
    await db.update_task("t1", estado=grafo.CORRIENDO, chat_id="c1")

    assert await orquestador.sanar(db, "g") == 1
    fila = (await db.get_task_graph("g"))["tasks"][0]
    assert fila["estado"] == grafo.ESPERANDO
    preguntas = await db.list_expert_questions(chat_id="c1")
    assert len(preguntas) == 1
    assert preguntas[0]["kind"] == "grafo"


async def test_sanar_no_toca_lo_que_ya_estaba_bien(db):
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "1"}, {"id": "t2", "titulo": "2"}])
    await db.update_task("t1", estado=grafo.HECHO, resultado="listo")

    assert await orquestador.sanar(db, "g") == 0
    por_id = {t["id"]: t for t in (await db.get_task_graph("g"))["tasks"]}
    assert por_id["t1"]["estado"] == grafo.HECHO
    assert por_id["t1"]["resultado"] == "listo"


async def test_sanar_es_idempotente(db):
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "1", "idempotente": True}])
    await db.update_task("t1", estado=grafo.CORRIENDO)
    assert await orquestador.sanar(db, "g") == 1
    assert await orquestador.sanar(db, "g") == 0


async def test_correr_un_grafo_cortado_lo_sana_y_lo_termina(db):
    """El caso completo de F4: el relay se cayó con un nodo a medias y
    al retomarlo el grafo llega hasta el final."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "uno", "idempotente": True},
        {"id": "t2", "titulo": "dos", "deps": ["t1"]}])
    await db.update_task("t1", estado=grafo.CORRIENDO)   # se cortó acá

    async def ejecutar(tarea):
        return {"ok": True, "resultado": "ok"}

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    assert prog["estado"] == "hecho" and prog["hechos"] == 2


# ---- la respuesta del humano mueve el grafo ----


async def _grafo_esperando(db):
    """Un grafo con `t1` frenada esperando al humano, como la deja el
    orquestador cuando una no idempotente falla."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "migrar"},
        {"id": "t2", "titulo": "cargar", "deps": ["t1"]}])
    await db.update_task("t1", estado=grafo.ESPERANDO, intentos=2,
                         error="reventó")
    return await db.get_task_graph("g")


async def test_reintentar_le_da_un_intento_mas(db):
    """Sin esto, 'reintentala igual' volvía a `pendiente` y la regla la
    mataba de nuevo en el acto —ya había gastado sus intentos— sin haber
    ejecutado nada. Desde afuera se ve como que el botón no hace nada."""
    await _grafo_esperando(db)
    assert await orquestador.aplicar_respuesta(db, "g", "t1", "reintentar") \
        == "reintentar"
    fila = next(t for t in (await db.get_task_graph("g"))["tasks"]
                if t["id"] == "t1")
    assert fila["estado"] == grafo.PENDIENTE
    assert fila["max_intentos"] > fila["intentos"], "no le quedó margen"

    corridas = []

    async def ejecutar(tarea):
        corridas.append(tarea["id"])
        return {"ok": True, "resultado": "ok"}

    prog = await orquestador.correr_grafo(db, "g", ejecutar=ejecutar)
    assert corridas == ["t1", "t2"], "el reintento tiene que EJECUTARSE"
    assert prog["estado"] == "hecho"


async def test_darla_por_fallada_bloquea_a_las_que_dependian(db):
    await _grafo_esperando(db)
    assert await orquestador.aplicar_respuesta(db, "g", "t1", "fallar") \
        == "fallar"
    por_id = {t["id"]: t for t in (await db.get_task_graph("g"))["tasks"]}
    assert por_id["t1"]["estado"] == grafo.FALLADO
    assert por_id["t2"]["estado"] == grafo.BLOQUEADO


async def test_parar_el_plan_lo_cancela(db):
    await _grafo_esperando(db)
    assert await orquestador.aplicar_respuesta(db, "g", "t1", "parar") == "parar"
    assert (await db.get_task_graph("g"))["estado"] == "cancelado"


async def test_contestar_dos_veces_no_aplica_dos_veces(db):
    """Discord y la Admin UI pueden contestar la misma pregunta."""
    await _grafo_esperando(db)
    assert await orquestador.aplicar_respuesta(db, "g", "t1", "fallar")
    assert await orquestador.aplicar_respuesta(db, "g", "t1", "reintentar") == ""
    fila = next(t for t in (await db.get_task_graph("g"))["tasks"]
                if t["id"] == "t1")
    assert fila["estado"] == grafo.FALLADO, "la segunda respuesta pisó a la primera"


async def test_la_decision_viaja_por_la_key_no_por_la_etiqueta(db):
    """Las opciones que deja el orquestador tienen que poder aplicarse
    sin comparar texto en castellano, que cambia el día que alguien lo
    redacte mejor."""
    import json as _json

    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "migrar", "chat_id": "c1"},
        {"id": "t2", "titulo": "cargar", "deps": ["t1"]}])
    await db.update_task("t1", estado=grafo.CORRIENDO, chat_id="c1")
    await orquestador.sanar(db, "g")

    q = _json.loads(
        (await db.list_expert_questions(chat_id="c1"))[0]["question_json"])
    assert q["task_id"] == "t1" and q["graph_id"] == "g"
    keys = [o["key"] for o in q["options"]]
    assert keys == ["reintentar", "fallar", "parar"]
    # Y cada key es aplicable tal cual.
    assert await orquestador.aplicar_respuesta(db, "g", "t1", keys[1]) == "fallar"


async def test_una_respuesta_de_ask_human_vuelve_a_la_tarea(db):
    """La otra clase de pregunta: el nodo preguntó algo que necesitaba
    saber. No decide qué hacer con la tarea — le da el dato."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "instalar", "detalle": "instalá lo que falte"},
        {"id": "t2", "titulo": "usar", "deps": ["t1"]}])
    await db.update_task("t1", estado=grafo.ESPERANDO, chat_id="chat9",
                         intentos=1)

    gid = await orquestador.responder_a_la_tarea(db, "chat9", "usá pnpm, no npm")
    assert gid == "g"
    fila = next(t for t in (await db.get_task_graph("g"))["tasks"]
                if t["id"] == "t1")
    assert fila["estado"] == grafo.PENDIENTE
    # El run siguiente tiene que VER la respuesta, o preguntaría de nuevo.
    assert "pnpm" in fila["detalle"]
    assert "instalá lo que falte" in fila["detalle"], "no pisó el detalle"


async def test_responder_a_un_chat_que_no_es_de_ninguna_tarea_no_hace_nada(db):
    assert await orquestador.responder_a_la_tarea(db, "chat_suelto", "x") == ""
    assert await orquestador.responder_a_la_tarea(db, "", "x") == ""


async def test_texto_libre_sin_opcion_le_llega_a_la_tarea(db):
    """Contestar con palabras, sin elegir ninguna de las opciones, tiene
    que valer: eso que escribió el humano es el dato que a la tarea le
    falta.

    Antes el texto se descartaba en silencio. La tarea volvía a
    `pendiente` con el detalle intacto y el nodo se re-ejecutaba
    IDÉNTICO — lo más probable, que fallara igual —, pero como el grafo
    sí se reanudaba, desde afuera parecía que había funcionado.
    """
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "migrar", "detalle": "ejecuta la migración"},
        {"id": "t2", "titulo": "cargar", "deps": ["t1"]}])
    await db.update_task("t1", estado=grafo.ESPERANDO, intentos=2,
                         error="reventó")

    assert await orquestador.aplicar_respuesta(
        db, "g", "t1", "", "la base es la de staging, no la de prod") \
        == "responder"

    fila = next(t for t in (await db.get_task_graph("g"))["tasks"]
                if t["id"] == "t1")
    assert fila["estado"] == grafo.PENDIENTE
    # Lo que importa: el reintento tiene que VER lo que dijo el humano.
    assert "staging" in fila["detalle"], "el texto del humano se perdió"
    assert "ejecuta la migración" in fila["detalle"], "pisó el detalle"
    assert fila["max_intentos"] > fila["intentos"], "no le quedó margen"


async def test_reintentar_sin_texto_deja_el_detalle_como_estaba(db):
    """El botón pelado no inventa una respuesta del humano."""
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "migrar", "detalle": "ejecuta la migración"}])
    await db.update_task("t1", estado=grafo.ESPERANDO, intentos=1)

    assert await orquestador.aplicar_respuesta(db, "g", "t1", "reintentar") \
        == "reintentar"
    fila = (await db.get_task_graph("g"))["tasks"][0]
    assert fila["detalle"] == "ejecuta la migración"


async def test_decision_y_texto_juntos_manda_la_decision_fallar(db):
    """`fallar` es terminal: el texto no la convierte en un reintento."""
    await _grafo_esperando(db)
    assert await orquestador.aplicar_respuesta(
        db, "g", "t1", "fallar", "ya lo hice a mano, no la repitas") \
        == "fallar"
    por_id = {t["id"]: t for t in (await db.get_task_graph("g"))["tasks"]}
    assert por_id["t1"]["estado"] == grafo.FALLADO
    assert por_id["t2"]["estado"] == grafo.BLOQUEADO


async def test_decision_y_texto_juntos_manda_la_decision_parar(db):
    """Y `parar` para de verdad, aunque venga con una explicación."""
    await _grafo_esperando(db)
    assert await orquestador.aplicar_respuesta(
        db, "g", "t1", "parar", "esto lo vemos mañana") == "parar"
    assert (await db.get_task_graph("g"))["estado"] == "cancelado"
    fila = next(t for t in (await db.get_task_graph("g"))["tasks"]
                if t["id"] == "t1")
    assert fila["estado"] == grafo.ESPERANDO, "no la revivió"


async def test_cada_nodo_reporta_su_progreso(db, tmp_path):
    """Los nodos del grafo no reportaban a nadie (2026-08-24).

    `ejecutor_minimax` recibía un `on_progress` suelto que ningún caller
    pasaba, y no podía servir igual: `make_progress_callback` registra el
    RunProgress bajo un `chat_id` y el chat del nodo se crea DENTRO del
    ejecutor. Resultado: `/experts/status/{chat_id}` contestaba "no hay
    run con ese id" y el panel no podía decir qué herramienta corría.
    Medido: veinte minutos de un nodo escribiendo archivos sin una sola
    señal en pantalla, y lo dimos por colgado.
    """
    await db.create_task_graph("g", "x", tareas=[
        {"id": "t1", "titulo": "Uno"}, {"id": "t2", "titulo": "Dos"}])

    pedidos: list = []          # los chat_id para los que se pidió callback
    eventos: list = []          # lo que el nodo reportó

    def progreso_de(chat_id: str):
        pedidos.append(chat_id)

        async def _cb(**kw):
            eventos.append((chat_id, kw.get("phase")))
        return _cb

    ejecutar = orquestador.ejecutor_minimax(
        db, _proyecto(tmp_path), await db.get_task_graph("g"),
        progreso_de=progreso_de)

    # El ejecutor real llamaría al modelo; nos alcanza con comprobar que
    # el callback llega armado y atado al chat del nodo.
    async def fake_run_expert(proyecto, prompt, **kw):
        cb = kw.get("on_progress")
        assert cb is not None, "el nodo corrió sin forma de reportar"
        assert kw.get("steer") is None  # Un callback personalizado no exige cola.
        await cb(phase="tool_call", tool="shell")
        return {"content": "ok", "phase_at_end": "writing", "model": "test",
                "tokens_in": 1, "tokens_out": 1, "tool_calls": 1,
                "duration_ms": 1, "messages_json": "[]"}

    from relay import experts
    orig = experts.run_expert
    experts.run_expert = fake_run_expert
    try:
        r1 = await ejecutar(dict((await db.list_tasks("g"))[0]))
    finally:
        experts.run_expert = orig

    assert r1["ok"] is True
    # Se pidió UN callback, para el chat de ESE nodo.
    assert len(pedidos) == 1 and pedidos[0]
    assert pedidos[0] == r1["chat_id"], "el callback no es del chat del nodo"
    assert eventos == [(pedidos[0], "tool_call")]


async def test_el_nodo_comparte_la_cola_steer_del_progreso(db, tmp_path, monkeypatch):
    """Un steer recibido después de arrancar llega a la misma lista del runner."""
    from relay import experts

    await db.create_task_graph("g-steer", "x", tareas=[{"id": "t1", "titulo": "Uno"}])
    store, seen = {}, []
    started, resume = asyncio.Event(), asyncio.Event()

    def progreso_de(chat_id):
        return progress.make_progress_callback(store, None, chat_id, "demo", "test")

    async def fake_run_expert(proyecto, prompt, **kw):
        queue = kw["steer"]
        assert queue is store[kw["chat_id"]].steer
        started.set()
        await resume.wait()
        seen.extend(queue)
        queue.clear()
        return {"content": "corregido", "phase_at_end": "writing", "model": "test"}

    monkeypatch.setattr(experts, "run_expert", fake_run_expert)
    graph = await db.get_task_graph("g-steer")
    ejecutar = orquestador.ejecutor_minimax(
        db, _proyecto(tmp_path), graph, progreso_de=progreso_de)
    task = asyncio.create_task(ejecutar(graph["tasks"][0]))
    try:
        await asyncio.wait_for(started.wait(), 1)
        run_progress = next(iter(store.values()))
        run_progress.steer.append("Deja de explorar y verifica el endpoint concreto.")
        resume.set()
        assert (await asyncio.wait_for(task, 1))["ok"] is True
        assert seen == ["Deja de explorar y verifica el endpoint concreto."]
        assert run_progress.steer == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelar_autosplit_conserva_resultado_del_ejecutor(db, tmp_path, monkeypatch):
    from relay import config, experts

    started = asyncio.Event()
    await db.create_task_graph("g-split-cancel", "x", tareas=[{"id": "t1", "titulo": "Uno"}])

    async def run_expert(*args, **kwargs):
        return {"content": "avance ya verificado", "phase_at_end": "budget_split",
                "model": "test", "tokens_in": 123, "tool_calls": 12,
                "messages_json": '[{"contenido":"historial del nodo"}]'}

    async def split(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(experts, "run_expert", run_expert)
    monkeypatch.setattr(orchestrator_execution, "_intentar_autosplit", split)
    monkeypatch.setattr(config, "grafo_autosplit_habilitado", lambda: True)
    graph = await db.get_task_graph("g-split-cancel")
    execute = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), graph)
    task = asyncio.create_task(execute(graph["tasks"][0]))
    try:
        await asyncio.wait_for(started.wait(), 1)
        chat_id = (await db.get_task_graph("g-split-cancel"))["tasks"][0]["chat_id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    chat = await db.get_chat(chat_id)
    assert chat["status"] == chat["phase_at_end"] == "cancelled"
    assert (chat["tokens_in"], chat["tool_calls"]) == (123, 12)
    payload = json.loads((await db.run(
        "SELECT payload FROM chat_outputs WHERE chat_id=?", (chat_id,)))[0]["payload"])
    assert payload["content"] == "avance ya verificado"
    assert "historial del nodo" in payload["messages_json"]


async def test_sin_factory_el_nodo_corre_igual(db, tmp_path):
    """La telemetría no puede ser un requisito: los tests y los callers
    viejos no pasan factory y el grafo tiene que correr igual."""
    await db.create_task_graph("g2", "x", tareas=[
        {"id": "a", "titulo": "A"}, {"id": "b", "titulo": "B"}])
    ejecutar = orquestador.ejecutor_minimax(
        db, _proyecto(tmp_path), await db.get_task_graph("g2"))

    async def fake_run_expert(proyecto, prompt, **kw):
        assert kw.get("on_progress") is None
        return {"content": "ok", "phase_at_end": "writing", "model": "test",
                "tokens_in": 1, "tokens_out": 1, "tool_calls": 0,
                "duration_ms": 1, "messages_json": "[]"}

    from relay import experts
    orig = experts.run_expert
    experts.run_expert = fake_run_expert
    try:
        r = await ejecutar(dict((await db.list_tasks("g2"))[0]))
    finally:
        experts.run_expert = orig
    assert r["ok"] is True


# ---------- la historia del nodo se guarda (2026-09-02) ----------

async def test_el_cierre_del_nodo_guarda_el_timeline(db):
    """`_cerrar_chat` tiene que pasar `progress_events` a `finish_chat`.

    Regresión medida el 2026-09-02: no los pasaba, y por eso el grafo
    NUNCA guardó la historia de sus nodos — de 234 nodos en la base, 0
    tenían `progress_events`; de 400 chats normales, 285 sí (el camino de
    `server.py` siempre los pasó). `run_expert` los devuelve desde
    siempre; el orquestador los tiraba.

    Duele más justo cuando el nodo se corta a mitad (`budget_exceeded`,
    `off_plan`): es cuando más se quiere saber hasta dónde llegó.
    """
    import json as _json

    capturado = {}

    class _DbEspia:
        async def get_chat(self, chat_id):
            return None

        async def finish_chat(self, chat_id, **kw):
            capturado["chat_id"] = chat_id
            capturado.update(kw)

    eventos = [{"ts": "2026-09-02T00:00:00Z", "phase": "tool_call",
                "tool": "shell", "cmd": "git status"}]
    await orquestador._cerrar_chat(
        _DbEspia(), "chat-1", "error", "el nodo terminó en 'budget_exceeded'",
        {"tokens_in": 7414144, "tokens_out": 84629, "tool_calls": 279,
         "phase_at_end": "budget_exceeded", "last_tool": "shell",
         "progress_events": eventos},
    )

    # Lo que ya andaba, sigue andando.
    assert capturado["status"] == "error"
    assert capturado["tool_calls"] == 279
    assert capturado["phase_at_end"] == "budget_exceeded"
    # Lo que faltaba.
    assert capturado["last_tool"] == "shell"
    assert _json.loads(capturado["progress_events"]) == eventos


async def test_el_cierre_sin_eventos_manda_lista_vacia(db):
    """Sin `progress_events` en el resultado, se persiste `[]` y no None.

    `finish_chat` hace COALESCE: un None dejaría el valor viejo, y en un
    reintento del mismo chat mostraría el timeline de la corrida anterior.
    """
    import json as _json

    capturado = {}

    class _DbEspia:
        async def get_chat(self, chat_id):
            return None

        async def finish_chat(self, chat_id, **kw):
            capturado.update(kw)

    await orquestador._cerrar_chat(_DbEspia(), "chat-2", "ok", "", {})
    assert _json.loads(capturado["progress_events"]) == []


# ---------- Fase 2B: autosplit de un nodo `budget_split` (2026-09-02) -------
#
# `budget_split` (Fase 1, tope de 250 tool calls sin supervisor) corta un
# nodo con trabajo real en curso. En vez de dejarlo fallado esperando al
# humano, se le pide a un modelo que reparta lo que FALTA en subtareas —
# reusando el mismo parser que arma los grafos (`planificador`), mockeado
# igual que en `test_planificador.py`: `experts.Agent`/`build_model`, no
# `_pedir_plan` directo (así se prueba de punta a punta, cascada incluida).


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
            vistos.append(prompt)
            return _Salida(quedan.pop(0))

    return _Agente, vistos


def _plan_json(tareas):
    import json as _json
    return _json.dumps({"tareas": tareas}, ensure_ascii=False)


async def test_budget_split_se_subdivide_y_reapunta_dependientes(db, tmp_path):
    """El caso central: un nodo con `parent_id=None` que corta con
    `budget_split` se reparte en subtareas — quedan con `parent_id`
    correcto y sus dependientes pasan a depender de ellas."""
    await db.create_task_graph("g", "objetivo grande", tareas=[
        {"id": "t1", "titulo": "la que se subdivide"},
        {"id": "t2", "titulo": "depende de t1", "deps": ["t1"]},
    ])
    g = await db.get_task_graph("g")
    t1 = next(t for t in g["tasks"] if t["id"] == "t1")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "avancé 213 tool calls, falta lo demás",
                "phase_at_end": "budget_split"}

    agente, vistos = _modelo_que_dice(_plan_json([
        {"id": "s1", "titulo": "sub 1"},
        {"id": "s2", "titulo": "sub 2", "deps": ["s1"]},
        {"id": "s3", "titulo": "sub 3"},
    ]))
    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert), \
            patch.object(experts, "Agent", agente), \
            patch.object(experts, "build_model", lambda spec: object()):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(t1)

    assert res["ok"] is False   # t1 en sí no terminó su trabajo
    assert "subdividido" in res["error"]
    # El prompt de subdivisión lleva lo YA hecho, para no repetirlo.
    assert "213 tool calls" in vistos[0]

    g2 = await db.get_task_graph("g")
    por_id = {t["id"]: t for t in g2["tasks"]}
    nuevas = [t for t in g2["tasks"] if t.get("parent_id") == "t1"]
    assert len(nuevas) == 3
    assert por_id["t2"]["deps"] == sorted(t["id"] for t in nuevas)
    assert "t1" not in por_id["t2"]["deps"]


async def test_una_subtarea_nunca_se_vuelve_a_subdividir(db, tmp_path):
    """Profundidad 1: un nodo con `parent_id` no se subdivide de nuevo,
    aunque él mismo corte con `budget_split`."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    await db.add_tasks_to_graph("g", [{"id": "s1", "titulo": "sub"}],
                                reemplaza="t1")
    g = await db.get_task_graph("g")
    s1 = next(t for t in g["tasks"] if t["id"] == "s1")
    assert s1["parent_id"] == "t1"

    llamado = {"n": 0}

    class _NoDeberiaLlamarse:
        def __init__(self, *a, **kw):
            llamado["n"] += 1

        async def run(self, prompt, **kw):
            raise AssertionError("no debería pedir plan de una subtarea")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "de nuevo sin presupuesto",
                "phase_at_end": "budget_split"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert), \
            patch.object(experts, "Agent", _NoDeberiaLlamarse):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(s1)

    assert llamado["n"] == 0
    assert res["ok"] is False
    assert "budget_split" in res["error"]


async def test_segundo_intento_sobre_nodo_ya_subdividido_no_repite(db, tmp_path):
    """Una sola vez por nodo, chequeado contra la BASE (no memoria del
    proceso): si ya hay subtareas con `parent_id=t1`, no se agregan más."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    await db.add_tasks_to_graph("g", [
        {"id": "s1", "titulo": "sub 1"}, {"id": "s2", "titulo": "sub 2"},
    ], reemplaza="t1")
    g = await db.get_task_graph("g")
    t1 = next(t for t in g["tasks"] if t["id"] == "t1")
    assert not t1.get("parent_id")   # t1 sigue siendo el nodo original

    llamado = {"n": 0}

    class _NoDeberiaLlamarse:
        def __init__(self, *a, **kw):
            llamado["n"] += 1

        async def run(self, prompt, **kw):
            raise AssertionError("no debería re-pedir plan")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "reintento", "phase_at_end": "budget_split"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert), \
            patch.object(experts, "Agent", _NoDeberiaLlamarse):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        await ejecutar(t1)

    assert llamado["n"] == 0
    g2 = await db.get_task_graph("g")
    nuevas = [t for t in g2["tasks"] if t.get("parent_id") == "t1"]
    assert len(nuevas) == 2, "se duplicó la subdivisión"


async def test_plan_ilegible_falla_como_budget_split_normal(db, tmp_path):
    """FALLA SEGURA: si el modelo no devuelve algo parseable, el nodo
    queda fallado exactamente como hoy — sin excepción, sin basura."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")
    t1 = g["tasks"][0]

    async def fake_run_expert(proj, user, **kw):
        return {"content": "avance parcial", "phase_at_end": "budget_split"}

    agente, _ = _modelo_que_dice("esto no es json ni por asomo")
    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert), \
            patch.object(experts, "Agent", agente), \
            patch.object(experts, "build_model", lambda spec: object()):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(t1)   # no debe propagar excepción

    assert res["ok"] is False
    assert "budget_split" in res["error"]
    g2 = await db.get_task_graph("g")
    assert len(g2["tasks"]) == 1, "no debía agregarse nada"


async def test_una_sola_tarea_no_es_subdivision(db, tmp_path):
    """Menos de 2 no es una subdivisión: si entraba en una tarea, no
    había que partirla (mismo criterio que `armar_grafo`)."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")
    t1 = g["tasks"][0]

    async def fake_run_expert(proj, user, **kw):
        return {"content": "avance", "phase_at_end": "budget_split"}

    agente, _ = _modelo_que_dice(_plan_json([{"id": "s1", "titulo": "sola"}]))
    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert), \
            patch.object(experts, "Agent", agente), \
            patch.object(experts, "build_model", lambda spec: object()):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        await ejecutar(t1)

    g2 = await db.get_task_graph("g")
    assert len(g2["tasks"]) == 1


async def test_fan_out_se_recorta_a_5(db, tmp_path):
    """Más de 5 es el modelo inventando trabajo: se recorta."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")
    t1 = g["tasks"][0]

    async def fake_run_expert(proj, user, **kw):
        return {"content": "avance", "phase_at_end": "budget_split"}

    nueve = [{"id": f"s{i}", "titulo": f"sub {i}"} for i in range(9)]
    agente, _ = _modelo_que_dice(_plan_json(nueve))
    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert), \
            patch.object(experts, "Agent", agente), \
            patch.object(experts, "build_model", lambda spec: object()):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        await ejecutar(t1)

    g2 = await db.get_task_graph("g")
    nuevas = [t for t in g2["tasks"] if t.get("parent_id") == "t1"]
    assert len(nuevas) == 5


async def test_flag_apagado_deja_el_comportamiento_viejo(db, tmp_path,
                                                          monkeypatch):
    """`FOURBIS_GRAFO_AUTOSPLIT=0`: ni se llama al planificador."""
    monkeypatch.setenv("FOURBIS_GRAFO_AUTOSPLIT", "0")
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "1"}])
    g = await db.get_task_graph("g")
    t1 = g["tasks"][0]

    llamado = {"n": 0}

    class _NoDeberiaLlamarse:
        def __init__(self, *a, **kw):
            llamado["n"] += 1

        async def run(self, prompt, **kw):
            raise AssertionError(
                "no debería llamar al planificador con el flag apagado")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "avance", "phase_at_end": "budget_split"}

    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert), \
            patch.object(experts, "Agent", _NoDeberiaLlamarse):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        res = await ejecutar(t1)

    assert llamado["n"] == 0
    assert res["ok"] is False
    assert res["error"] == "el nodo terminó en 'budget_split'"
    g2 = await db.get_task_graph("g")
    assert len(g2["tasks"]) == 1


async def test_las_subtareas_heredan_los_archivos_del_nodo_original(db, tmp_path):
    """Sin herencia, dos subtareas del mismo nodo pueden pisarse.

    `tasks.archivos` es lo que el scheduler usa para no co-agendar dos
    tareas que tocan lo mismo (ver `grafo.elegibles` y las reservas de
    `files.Permisos`). Un modelo de subdivisión que no los declara
    dejaría a las subtareas SIN archivos, o sea "no tocan nada" para el
    scheduler — y dos que escriben el mismo archivo arrancarían juntas.

    Hueco de cobertura encontrado el 2026-09-02: el comportamiento
    estaba bien implementado pero ningún test lo guardaba.
    """
    await db.create_task_graph("g", "objetivo", tareas=[
        {"id": "t1", "titulo": "la grande",
         "archivos": ["src/a.py", "src/b.py"]},
    ])
    g = await db.get_task_graph("g")
    t1 = next(t for t in g["tasks"] if t["id"] == "t1")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "avancé la mitad", "phase_at_end": "budget_split"}

    # s1 no declara archivos → hereda; s2 declara los suyos → NO se pisan.
    agente, _ = _modelo_que_dice(_plan_json([
        {"id": "s1", "titulo": "sub sin archivos"},
        {"id": "s2", "titulo": "sub con los suyos",
         "archivos": ["src/c.py"]},
    ]))
    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert), \
            patch.object(experts, "Agent", agente), \
            patch.object(experts, "build_model", lambda spec: object()):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        await ejecutar(t1)

    import json as _json

    nuevas = {t["titulo"]: t for t in (await db.get_task_graph("g"))["tasks"]
              if t.get("parent_id") == "t1"}
    assert len(nuevas) == 2, nuevas

    def _archivos(t):
        # `tasks.archivos` viaja como TEXT/JSON crudo, no como lista ya
        # parseada — es lo que espera `_archivos_de` en orquestador.
        v = t["archivos"]
        return _json.loads(v) if isinstance(v, str) else list(v or [])

    heredada = _archivos(nuevas["sub sin archivos"])
    propia = _archivos(nuevas["sub con los suyos"])
    assert sorted(heredada) == ["src/a.py", "src/b.py"], heredada
    # La herencia NO pisa lo que el modelo sí declaró.
    assert propia == ["src/c.py"], propia


async def test_los_ids_de_subtarea_no_duplican_el_prefijo_del_grafo(db, tmp_path):
    """`_ids_unicos` antepone su parámetro, y `tarea["id"]` YA trae el grafo.

    Visto en la prueba en vivo del 2026-09-03: el prefijo salía dos veces
    (`g_x:g_x:t1:split:s1`). No rompía nada —los ids son opacos— pero se
    leen en la UI, en los logs y en `task_deps`.
    """
    # El id de la tarea lleva el prefijo del grafo, como en producción
    # (`planificador._ids_unicos` los prefija al armar el grafo). Sin
    # eso el test no discrimina: con un id pelado tipo "t1", el código
    # viejo daba "g:t1:split:uno", que igual parece razonable.
    await db.create_task_graph("g", "objetivo", tareas=[
        {"id": "g:t1", "titulo": "la grande"},
    ])
    g = await db.get_task_graph("g")
    t1 = next(t for t in g["tasks"] if t["id"] == "g:t1")

    async def fake_run_expert(proj, user, **kw):
        return {"content": "a medias", "phase_at_end": "budget_split"}

    agente, _ = _modelo_que_dice(_plan_json([
        {"id": "uno", "titulo": "sub uno"},
        {"id": "dos", "titulo": "sub dos", "deps": ["uno"]},
    ]))
    from relay import experts
    with patch.object(experts, "run_expert", fake_run_expert), \
            patch.object(experts, "Agent", agente), \
            patch.object(experts, "build_model", lambda spec: object()):
        ejecutar = orquestador.ejecutor_minimax(db, _proyecto(tmp_path), g)
        await ejecutar(t1)

    hijas = [t["id"] for t in (await db.get_task_graph("g"))["tasks"]
             if t.get("parent_id") == "g:t1"]
    assert len(hijas) == 2, hijas
    for hid in hijas:
        # Con el bug, el prefijo del grafo salía dos veces: "g:g:t1:...".
        assert not hid.startswith("g:g:"), hid
        assert hid.count("g:") == 1, hid
    assert sorted(hijas) == ["g:t1:split:dos", "g:t1:split:uno"], hijas


# ---------- regresión: cierre de nodo de grafo debe persistir la transcripción ----------

@pytest.mark.asyncio
async def test_cerrar_chat_de_nodo_de_grafo_escribe_md_y_jsonl(tmp_path, monkeypatch):
    """Cierre de un nodo de grafo debe dejar artefactos persistidos.

    Bug medido (ea92dbb, 2026-09-06): en `~/.4bis/relay.db` hay 1538 chats;
    327 sin md_path, 295 de source='grafo'. Cero .md en ese origen (los
    demás orígenes suman 1107+72+17+9). Consumieron ~389M tokens de input
    sin dejar transcripción.

    Causa: `_run_expert_bg` (server.py) escribe `write_chat_md` +
    `append_jsonl` al cerrar; `_cerrar_chat` (orquestador.py) solo llama
    `db.finish_chat`. Hasta hoy el camino del grafo no tocaba los
    helpers de `persist`.

    Esta prueba es la regresión: si `_cerrar_chat` deja de invocar
    `write_chat_artifacts_from_chat`, el md_path queda en None y los
    archivos no existen. La medición importa porque 'no escribió nada'
    es silencioso — la base registra la corrida y los tokens pero nadie
    puede releer qué hizo el experto.
    """
    from relay import config, persist
    from relay.orquestador import _cerrar_chat

    chats_dir = tmp_path / "chats"
    jsonl_dir = tmp_path / "jsonl"
    chats_dir.mkdir()
    jsonl_dir.mkdir()
    monkeypatch.setattr(config, "chats_dir", lambda: chats_dir)
    monkeypatch.setattr(config, "jsonl_dir", lambda: jsonl_dir)

    d = tmp_path / "relay.db"
    db = Database(path=d)
    await db.init_schema()

    chat_id = await db.create_chat(
        project_slug=None,
        source="grafo",
        author="nemotron",
        target="main",
        user_prompt="hacé X",
    )
    # `db.create_chat` devuelve un uuid; para asserts conviene uno fijo
    # pero no es necesario: usamos el real.

    res = {
        "content": "## Plan\n- paso 1\n- paso 2\n\n## Veredicto\nlisto.",
        "status": "ok",
        "model": "minimax-m3",
        "duration_ms": 1234,
    }

    await _cerrar_chat(db, chat_id, "ok", "", res)

    row = await db.get_chat(chat_id)
    md_path = row.get("md_path")
    assert md_path, f"cerrar un nodo de grafo debe dejar md_path en la fila; row={row}"
    md_file = chats_dir / "main" / Path(md_path).name
    assert md_file.exists(), md_file

    md_text = md_file.read_text(encoding="utf-8")
    assert "paso 1" in md_text
    # El prompt tambien: sin esto el .md trae la respuesta sin la
    # pregunta. Es lo que dejaba pasar el bug de `row.get("user")`.
    assert "hacé X" in md_text, "el .md tiene que traer la tarea del nodo"
    assert "paso 2" in md_text

    jsonl_path = jsonl_dir / "main.jsonl"
    assert jsonl_path.exists(), jsonl_path
    lineas = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert any('"role":"assistant"' in ln for ln in lineas), lineas[:1]
    assert any("paso 1" in ln for ln in lineas), lineas[:1]


async def test_dos_escritores_nunca_corren_juntos_aunque_toquen_otro_archivo(db):
    """La garantia nueva, sobre el loop real y no sobre `elegibles`.

    `Permisos.reservadas` protege las tools de archivo, pero la `shell`
    corre un comando arbitrario y puede escribir cualquier cosa: un nodo
    con `a.py` reservado no tenia nada que le impidiera hacer `sed -i`
    sobre el `b.py` de su hermano. Filtrar comandos no es opcion y
    sacarle la shell a los nodos con hermanos vivos rompe al de
    verificacion, que existe para correr el build. Queda serializar a
    los que declaran que van a escribir.
    """
    await db.create_task_graph("g", "x", tareas=[
        {"id": f"t{i}", "titulo": str(i), "archivos": [f"f{i}.py"]}
        for i in range(4)])
    vivos: set = set()
    maximo = {"n": 0}

    async def ejecutar(tarea):
        vivos.add(tarea["id"])
        # Igual que su contrapeso: darle tiempo real a que aparezca una
        # companera. Si el motor deja entrar a dos escritores, acá se ve.
        for _ in range(40):
            if len(vivos) >= 2:
                break
            await asyncio.sleep(0.005)
        maximo["n"] = max(maximo["n"], len(vivos))
        vivos.discard(tarea["id"])
        return {"ok": True, "resultado": "ok"}

    await orquestador.correr_grafo(db, "g", ejecutar=ejecutar, tope=2)
    assert maximo["n"] == 1, (
        f"corrieron {maximo['n']} escritores a la vez sobre el mismo repo")


async def test_el_grafo_es_serial_por_defecto(db):
    """Ni siquiera dos nodos que no declaran archivos van juntos.

    `conflictan` serializa a los que declaran, pero un nodo con
    `archivos=[]` conserva la shell: dos `npm run build` sobre el mismo
    working tree se corrompen sin que nadie declare nada. Medido sobre
    los 339 nodos historicos, 111 (33%) no declaran archivos y 29 pares
    de esos llegaron a solaparse.
    """
    await db.create_task_graph("g", "x", tareas=[
        {"id": f"t{i}", "titulo": str(i)} for i in range(4)])
    vivos: set = set()
    maximo = {"n": 0}

    async def ejecutar(tarea):
        vivos.add(tarea["id"])
        for _ in range(40):
            if len(vivos) >= 2:
                break
            await asyncio.sleep(0.005)
        maximo["n"] = max(maximo["n"], len(vivos))
        vivos.discard(tarea["id"])
        return {"ok": True, "resultado": "ok"}

    await orquestador.correr_grafo(
        db, "g", ejecutar=ejecutar, tope=orquestador.TOPE_PARALELO)
    assert maximo["n"] == 1, f"corrieron {maximo['n']} nodos a la vez"


async def test_lanzar_saca_el_tope_del_flag_del_proyecto(db, monkeypatch):
    """`grafo_paralelo` es opt-in: sin el, serial; con el, sube el tope."""
    await db.create_task_graph("g", "x", tareas=[{"id": "t1", "titulo": "a"}])
    topes: list = []

    async def falso_correr(db_, gid, **kw):
        topes.append(kw.get("tope"))
        return {"estado": "hecho"}

    monkeypatch.setattr(orquestador, "correr_grafo", falso_correr)
    await orquestador.lanzar(db, {"slug": "demo", "defaults_json": {}}, "g")
    await orquestador.lanzar(
        db, {"slug": "demo", "defaults_json": {"grafo_paralelo": True}}, "g")
    assert topes == [orquestador.TOPE_PARALELO,
                     orquestador.TOPE_PARALELO_OPTIN], topes
    assert topes[0] == 1, "el default dejo de ser serial"


async def test_el_verificador_del_grafo_recibe_los_comandos_de_sus_nodos(db):
    """Recibia `tool_calls_summary: []` y el comentario del codigo lo admitia.

    El rastro existia: cada nodo deja en `progress_events` el comando en
    su `tool_call` y el exit code al final del `tool_result`. Sin eso, el
    verificador del grafo no podia distinguir "corrio el build" de "el
    build paso" — el mismo agujero que el del turno.
    """
    eventos = [
        {"phase": "tool_call", "tool": "shell",
         "cmd": "npm run build\n# cwd: frontend"},
        {"phase": "tool_result", "tool": "shell", "output": "ok\n(exit=0)"},
        {"phase": "tool_call", "tool": "shell", "cmd": "pytest -q"},
        {"phase": "tool_result", "tool": "shell",
         "output": "1 failed\n(exit=1)"},
    ]

    class _DB:
        async def get_chat(self, _cid):
            return {"progress_events": json.dumps(eventos)}

    g = {"tasks": [{"id": "t1", "titulo": "construir", "chat_id": "c1",
                    "estado": "hecho"}]}
    raw = await orquestador._evidencia_de_los_nodos(_DB(), g)

    from relay.experts import Bitacora
    ev = Bitacora.cargar(raw).evidencia()
    assert "npm run build → exit=0" in ev
    assert "pytest -q → exit=1" in ev
    # El "# cwd:" de la segunda linea del cmd no se cuela.
    assert "cwd" not in ev


async def test_la_evidencia_del_grafo_no_repite_el_estado_de_los_nodos(db):
    """El estado y el error ya van en el `content` via `_resumen_de_nodos`.

    Anotarlos tambien como evidencia los duplicaria y, peor, los meteria
    bajo "lo que el ejecutor dice haber comprobado" — la etiqueta de las
    afirmaciones del modelo. El estado de un nodo es un hecho del
    harness.
    """
    class _DB:
        async def get_chat(self, _cid):
            return {"progress_events": "[]"}

    g = {"tasks": [{"id": "t1", "titulo": "probar", "chat_id": None,
                    "estado": "fallado"}]}
    assert await orquestador._evidencia_de_los_nodos(_DB(), g) == ""


async def test_un_chat_ilegible_no_voltea_la_verificacion_del_grafo(db):
    """Sin evidencia se verifica peor, no se rompe el cierre."""
    class _DB:
        async def get_chat(self, _cid):
            raise RuntimeError("la base no esta")

    g = {"tasks": [{"id": "t1", "titulo": "x", "chat_id": "c1",
                    "estado": "hecho"}]}
    assert await orquestador._evidencia_de_los_nodos(_DB(), g) == ""


async def test_el_cierre_del_grafo_le_pasa_la_evidencia_al_verificador():
    """El enganche, no la funcion: que el payload la lleve de verdad.

    Un test sobre `_evidencia_de_los_nodos` sola pasa igual aunque nadie
    la llame — que era exactamente el estado anterior.
    """
    eventos = [
        {"phase": "tool_call", "tool": "shell", "cmd": "npm run build"},
        {"phase": "tool_result", "tool": "shell", "output": "ok\n(exit=0)"},
    ]
    vistos: list = []

    class _DB:
        async def get_chat(self, _cid):
            return {"progress_events": json.dumps(eventos)}

        async def set_task_graph_verificacion(self, *a):
            return None

    async def falso_verificar(**kw):
        vistos.append(kw.get("executor_result") or {})
        return {"verdict": "complete", "feedback": "ok", "modelo": "test"}

    g = {"objetivo": "x", "tasks": [
        {"id": "t1", "titulo": "construir", "chat_id": "c1",
         "estado": "hecho", "deps": [], "orden": 0}]}
    await orquestador._verificar_al_cerrar(
        _DB(), "g", g, {"estado": "hecho"}, falso_verificar)

    assert vistos, "no se llamo al verificador"
    from relay.experts import Bitacora
    raw = vistos[0].get("bitacora_json") or ""
    assert "exit=0" in Bitacora.cargar(raw).evidencia(), (
        "el payload del verificador del grafo va sin evidencia")
