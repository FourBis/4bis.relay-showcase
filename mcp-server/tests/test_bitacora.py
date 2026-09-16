"""Bitácora del run (2026-08-17): lo verificado sobrevive a la elisión.

El bug que motiva esto: con `TOOL_KEEP_FULL=8`, en un run de 172 tool
calls el experto ve 8 results y 164 muñones, así que reporta como hecho
lo que no puede releer. La bitácora es el lugar chico y durable donde
acumula lo comprobado; lo que se testea acá son sus topes (que existen
para no reintroducir el N²) y que el recorte se coma lo viejo y no lo
último verificado.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay.experts import Bitacora  # noqa: E402


def test_anota_y_renderiza():
    b = Bitacora()
    b.anotar("docker 29.6.1 responde")
    out = b.render()
    assert "docker 29.6.1 responde" in out
    assert "YA verificaste" in out


def test_vacia_no_gasta_tokens():
    # "" y no un encabezado huérfano: un bloque vacío se paga igual.
    assert Bitacora().render() == ""


def test_no_duplica():
    b = Bitacora()
    b.anotar("el build pasó en 7.66s")
    b.anotar("el build  pasó   en 7.66s")  # mismo hecho, otro espaciado
    assert len(b.hechos) == 1


def test_hecho_vacio_no_entra():
    b = Bitacora()
    b.anotar("   ")
    b.anotar("")
    assert b.hechos == []


def test_tope_de_entradas_tira_lo_viejo():
    b = Bitacora()
    for i in range(Bitacora.MAX + 10):
        b.anotar(f"hecho {i}")
    assert len(b.hechos) == Bitacora.MAX
    assert "hecho 0" not in b.hechos          # lo viejo se cayó
    assert f"hecho {Bitacora.MAX + 9}" in b.hechos  # lo último quedó


def test_render_respeta_el_techo_de_chars():
    b = Bitacora()
    for i in range(Bitacora.MAX):
        b.anotar(f"hecho {i} " + "x" * 200)
    out = b.render()
    # El techo es sobre lo que se re-manda; el encabezado y el cierre
    # van aparte, así que damos margen y verificamos el orden de
    # magnitud, no el byte exacto.
    assert len(out) < Bitacora.MAX_CHARS + 500
    assert "omitidos por espacio" in out


def test_al_recortar_sobrevive_lo_ultimo_no_lo_primero():
    b = Bitacora()
    for i in range(Bitacora.MAX):
        b.anotar(f"hecho {i} " + "x" * 200)
    out = b.render()
    ultimo = f"hecho {Bitacora.MAX - 1} "
    assert ultimo in out, "se recortó lo recién verificado"
    assert "hecho 0 " not in out, "sobrevivió lo viejo en vez de lo nuevo"


def test_orden_cronologico_en_el_render():
    b = Bitacora()
    b.anotar("primero")
    b.anotar("segundo")
    out = b.render()
    assert out.index("primero") < out.index("segundo")


def test_comando_queda_con_su_exit_code():
    b = Bitacora()
    b.anotar_comando("docker --version", 0)
    out = b.render()
    assert "docker --version" in out
    assert "exit=0" in out


def test_comando_fallido_tambien_se_registra():
    b = Bitacora()
    b.anotar_comando("npm run lint", 1)
    assert "exit=1" in b.render()


def test_los_comandos_no_desalojan_los_hechos():
    """El motivo del ring aparte: 172 comandos vaciarían los 60 hechos."""
    b = Bitacora()
    b.anotar("31 de 40 capturas con el mismo sha256")
    for i in range(200):
        b.anotar_comando(f"echo {i}", 0)
    out = b.render()
    assert "31 de 40 capturas" in out, "el log de comandos se comió el hecho"
    assert len(b.comandos) == Bitacora.MAX_COMANDOS
    assert "echo 199" in out            # queda lo último
    assert "echo 0 " not in out         # se fue lo viejo


def test_render_dice_que_el_log_lo_escribe_el_harness():
    """Es la línea que corta el "confirmame vos que corrió el comando"."""
    b = Bitacora()
    b.anotar_comando("docker --version", 0)
    assert "harness" in b.render()


def test_marcar_paso_numerico_anota_y_rinde():
    b = Bitacora()
    b.marcar_paso(1, "leí docs/foo.md")
    b.marcar_paso(3, "modifiqué server.py")
    assert b.pasos == {1: "leí docs/foo.md", 3: "modifiqué server.py"}
    dump = json.loads(b.volcar())
    assert dump["pasos"]["1"] == "leí docs/foo.md"
    assert dump["pasos"]["3"] == "modifiqué server.py"


def test_marcar_paso_sin_nota_acepta_string_vacio():
    b = Bitacora()
    b.marcar_paso(2, "")
    assert b.pasos == {2: ""}


def test_marcar_paso_fuera_de_rango_no_falla():
    """Un número mal puesto no puede tirar el run."""
    b = Bitacora()
    b.marcar_paso(99, "no existe")
    # La marcamos igual: filtrar es responsabilidad del orquestador
    # contra el plan real del run, no de la Bitácora. Lo que probamos
    # acá es que la anotación no rompe nada.
    assert b.pasos == {99: "no existe"}


def test_marcar_paso_resetea_con_cargar():
    """El `bitacora_json` ya se persiste entre turnos; los pasos también."""
    b = Bitacora()
    b.marcar_paso(1, "primero")
    b2 = Bitacora.cargar(b.volcar())
    b2.marcar_paso(2, "segundo")
    assert b2.pasos == {1: "primero", 2: "segundo"}
    # Un turno nuevo no pierde los pasos del anterior.
    assert b2.pasos[1] == "primero"
    assert json.loads(b.volcar())["pasos"]["1"] == "primero"


def test_instructions_dinamicas_llegan_frescas_al_modelo():
    """El enganche, que es lo que se rompe callado en un upgrade.

    Todo el diseño depende de que pydantic-ai re-evalúe los callables de
    `instructions=` en CADA request. Si un upgrade los congelara en el
    primer run, la bitácora seguiría llenándose y el modelo seguiría sin
    verla — exactamente el bug que esto vino a arreglar, pero mudo.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.messages import ModelRequest

    b = Bitacora()
    agent = Agent(TestModel(), instructions=["prompt estable", b.render])

    b.anotar("docker 29.6.1 responde")
    r1 = agent.run_sync("hola")
    ins1 = [m.instructions for m in r1.all_messages()
            if isinstance(m, ModelRequest)][-1]
    assert "prompt estable" in ins1
    assert "docker 29.6.1 responde" in ins1

    # Lo anotado DESPUÉS del primer request tiene que aparecer en el
    # siguiente, sobre el mismo historial.
    b.anotar("31 de 40 capturas con el mismo sha256")
    r2 = agent.run_sync("seguí", message_history=r1.all_messages())
    ins2 = [m.instructions for m in r2.all_messages()
            if isinstance(m, ModelRequest)][-1]
    assert "31 de 40 capturas" in ins2
    assert "docker 29.6.1 responde" in ins2
