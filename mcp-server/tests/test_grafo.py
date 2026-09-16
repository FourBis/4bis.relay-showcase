"""El plan como grafo de tareas (F1, 2026-08-17).

Lo caro de este módulo no es guardar filas: es **qué corre, qué se
bloquea y qué se reintenta**. Esas tres decisiones son las que, si están
mal, se descubren con trabajo a medio hacer encima — así que van
probadas a fondo y con la lógica separada de la DB para poder hacerlo.

Las dos reglas que acordamos con el usuario y que estos tests fijan:

  1. Un nodo que falla BLOQUEA a los que dependían de él (transitivo),
     en vez de dejar que el resto siga sobre una base rota.
  2. Reintento automático **solo** para nodos declarados idempotentes.
     Los que escriben paran y preguntan: reintentar algo que ya escribió
     archivos puede duplicar el efecto, y eso el runtime no lo sabe.
"""
from __future__ import annotations

import pytest

from relay import grafo
from relay.grafo import BLOQUEADO, CORRIENDO, FALLADO, HECHO, Nodo, PENDIENTE


def n(id_, *deps, estado=PENDIENTE, **kw):
    return Nodo(id=id_, titulo=id_, deps=tuple(deps), estado=estado, **kw)


# ---------- 1. validación: lo que un LLM puede escribir mal ----------


def test_un_grafo_sano_pasa():
    grafo.validar([n("a"), n("b", "a"), n("c", "a", "b")])


def test_dependencia_a_un_id_que_no_existe():
    with pytest.raises(grafo.GrafoInvalido) as e:
        grafo.validar([n("a"), n("b", "fantasma")])
    assert "fantasma" in str(e.value)


def test_ids_repetidos():
    with pytest.raises(grafo.GrafoInvalido) as e:
        grafo.validar([n("a"), n("a")])
    assert "repetidos" in str(e.value)


def test_un_nodo_que_depende_de_si_mismo():
    with pytest.raises(grafo.GrafoInvalido):
        grafo.validar([n("a", "a")])


def test_ciclo_y_el_error_dice_cual():
    """El mensaje va a un LLM que tiene que corregirlo: 'hay un ciclo' no
    le dice qué arista romper."""
    with pytest.raises(grafo.GrafoInvalido) as e:
        grafo.validar([n("a", "c"), n("b", "a"), n("c", "b")])
    msg = str(e.value)
    assert "ciclo" in msg
    assert "a" in msg and "b" in msg and "c" in msg


def test_grafo_vacio():
    with pytest.raises(grafo.GrafoInvalido):
        grafo.validar([])


def test_estado_desconocido():
    with pytest.raises(grafo.GrafoInvalido):
        grafo.validar([Nodo(id="a", titulo="a", estado="volando")])


# ---------- 2. orden topológico ----------


def test_orden_respeta_las_dependencias():
    orden = grafo.orden_topologico([n("c", "b"), n("b", "a"), n("a")])
    assert orden == ["a", "b", "c"]


def test_el_orden_es_estable_entre_corridas():
    """Sin desempate estable la UI reordena los nodos entre refrescos y
    parece que pasó algo."""
    nodos = [n("z", orden=1), n("y", orden=0), n("x", orden=0)]
    assert grafo.orden_topologico(nodos) == grafo.orden_topologico(nodos)
    assert grafo.orden_topologico(nodos) == ["x", "y", "z"]


def test_diamante():
    orden = grafo.orden_topologico(
        [n("fin", "izq", "der"), n("izq", "ini"), n("der", "ini"), n("ini")])
    assert orden[0] == "ini" and orden[-1] == "fin"


# ---------- 3. qué se puede lanzar ahora ----------


def test_listas_solo_las_que_tienen_todo_hecho():
    nodos = [n("a", estado=HECHO), n("b", "a"), n("c", "b")]
    assert [x.id for x in grafo.listas(nodos)] == ["b"]


def test_varias_a_la_vez_es_el_punto_del_dag():
    nodos = [n("raiz", estado=HECHO), n("a", "raiz"), n("b", "raiz")]
    assert [x.id for x in grafo.listas(nodos)] == ["a", "b"]


def test_una_dependencia_a_medias_no_habilita():
    nodos = [n("a", estado=CORRIENDO), n("b", "a")]
    assert grafo.listas(nodos) == []


def test_lo_que_ya_corre_no_se_relanza():
    """Sin esto el orquestador lanzaría dos runs para la misma tarea."""
    assert grafo.listas([n("a", estado=CORRIENDO)]) == []


# ---------- 4. bloqueo en cascada (regla 1) ----------


def test_un_fallo_bloquea_a_los_que_dependian():
    nodos = [n("a", estado=FALLADO), n("b", "a"), n("c", "b")]
    assert grafo.bloqueados_por(nodos, "a") == ["b", "c"]


def test_no_bloquea_a_una_rama_independiente():
    """Lo que una lista ordenada no puede hacer: seguir con lo que sí sirve."""
    nodos = [n("a", estado=FALLADO), n("b", "a"), n("otro")]
    assert grafo.bloqueados_por(nodos, "a") == ["b"]


def test_lo_que_ya_termino_no_se_pisa():
    """Un nodo que alcanzó a terminar bien hizo su trabajo; marcarlo
    bloqueado borraría el resultado."""
    nodos = [n("a", estado=FALLADO), n("b", "a", estado=HECHO), n("c", "b")]
    assert grafo.bloqueados_por(nodos, "a") == ["c"]


def test_bloqueo_sin_dependientes():
    assert grafo.bloqueados_por([n("a", estado=FALLADO)], "a") == []


# ---------- 5. reintentos (regla 2) ----------


def test_un_nodo_idempotente_se_reintenta_solo():
    assert grafo.decidir_tras_fallo(
        n("a", idempotente=True, intentos=0)) == "reintentar"


def test_un_nodo_que_escribe_para_y_pregunta():
    """El default es NO reintentar: reintentar algo que ya escribió
    archivos puede duplicar el efecto, y el runtime no puede saber si el
    efecto ya ocurrió. El que sabe es quien escribió la tarea."""
    assert grafo.decidir_tras_fallo(
        n("a", idempotente=False, intentos=0)) == "preguntar"


def test_agotados_los_intentos_falla_aunque_sea_idempotente():
    assert grafo.decidir_tras_fallo(
        n("a", idempotente=True, intentos=2, max_intentos=2)) == "fallar"


def test_el_default_de_idempotente_es_false():
    """Ante la duda, para y pregunta."""
    assert Nodo(id="a", titulo="a").idempotente is False


# ---------- 6. estado del grafo ----------


def test_todo_hecho():
    assert grafo.estado_del_grafo(
        [n("a", estado=HECHO), n("b", estado=HECHO)]) == "hecho"


def test_activo_mientras_haya_algo_lanzable():
    assert grafo.estado_del_grafo([n("a"), n("b", "a")]) == "activo"


def test_fallado_cuando_no_queda_nada_por_hacer():
    nodos = [n("a", estado=FALLADO), n("b", "a", estado=BLOQUEADO)]
    assert grafo.estado_del_grafo(nodos) == "fallado"


def test_una_rama_sana_mantiene_el_grafo_activo():
    """Un fallo no cancela el trabajo que todavía sirve."""
    nodos = [n("a", estado=FALLADO), n("b", "a", estado=BLOQUEADO), n("otro")]
    assert grafo.estado_del_grafo(nodos) == "activo"


def test_esperando_humano_mantiene_activo():
    """Está en pausa esperando una decisión, no muerto."""
    nodos = [n("a", estado=grafo.ESPERANDO), n("b", "a")]
    assert grafo.estado_del_grafo(nodos) == "activo"


def test_progreso_cuenta_bloqueados_aparte_de_fallados():
    """Contar bloqueados como fallas infla el número y esconde la causa."""
    p = grafo.progreso([n("a", estado=FALLADO), n("b", "a", estado=BLOQUEADO),
                        n("c", estado=HECHO)])
    assert p["fallados"] == 1
    assert p["bloqueados"] == 1
    assert p["hechos"] == 1
    assert p["porcentaje"] == 100      # los tres cerrados


@pytest.mark.parametrize("estado_hijo,estado_grafo,porcentaje,fallados", [
    (HECHO, "hecho", 100, 0),
    (PENDIENTE, "activo", 50, 0),
    (CORRIENDO, "activo", 50, 0),
    (grafo.ESPERANDO, "activo", 50, 0),
    (FALLADO, "fallado", 100, 1),
    (BLOQUEADO, "fallado", 100, 0),
])
def test_split_cuenta_los_hijos_sin_convertir_el_padre_en_exito(
        estado_hijo, estado_grafo, porcentaje, fallados):
    padre = n("original", estado=FALLADO)
    nodos = [padre, n("sub1", estado=HECHO, parent_id=padre.id),
             n("sub2", "sub1", estado=estado_hijo, parent_id=padre.id)]
    p = grafo.progreso(nodos)
    assert grafo.sustituidos(nodos) == {padre.id}
    assert p["estado"] == estado_grafo
    assert p["total_historico"] == 3
    assert p["sustituidos"] == 1
    assert p["total"] == 2
    assert p["hechos"] == (2 if estado_hijo == HECHO else 1)
    assert p["fallados"] == fallados
    assert p["porcentaje"] == porcentaje
    assert sum(p[k] for k in ("hechos", "corriendo", "pendientes",
                              "bloqueados", "fallados", "esperando_humano")) == p["total"]
    assert padre.estado == FALLADO  # historial intacto


def test_split_no_oculta_fallos_de_otras_ramas():
    nodos = [n("original", estado=FALLADO),
             n("sub", estado=HECHO, parent_id="original"),
             n("otra", estado=FALLADO)]
    p = grafo.progreso(nodos)
    assert p["estado"] == "fallado"
    assert p["fallados"] == 1
    assert p["sustituidos"] == 1


@pytest.mark.parametrize("nodos", [
    [n("original", estado=FALLADO), n("sub", estado=HECHO)],
    [n("original", estado=FALLADO),
     n("sub", "original", estado=BLOQUEADO, parent_id="original")],
    [n("original", estado=CORRIENDO),
     n("sub", estado=HECHO, parent_id="original")],
    [n("original", estado=FALLADO, parent_id="original")],
    [n("sub", estado=FALLADO, parent_id="inexistente")],
])
def test_solo_se_sustituye_un_padre_cerrado_con_reapunte_completo(nodos):
    assert grafo.sustituidos(nodos) == set()
    p = grafo.progreso(nodos)
    assert p["total"] == p["total_historico"] == len(nodos)
    assert p["sustituidos"] == 0


def test_split_historico_se_muestra_terminado_y_respeta_cancelacion():
    nodos = [n("original", estado=FALLADO),
             n("sub", estado=HECHO, parent_id="original")]
    assert grafo.estado_visible(nodos, "fallado") == "hecho"
    assert grafo.estado_visible(nodos, "cancelado") == "cancelado"


def test_splits_anidados_se_cuentan_una_vez_sin_ocultar_linajes_circulares():
    nodos = [n("original", estado=FALLADO),
             n("sub", estado=FALLADO, parent_id="original"),
             n("nieto", estado=HECHO, parent_id="sub")]
    assert grafo.sustituidos(nodos) == {"original", "sub"}
    assert grafo.progreso(nodos)["estado"] == "hecho"
    assert grafo.progreso(nodos)["total"] == 1

    # Dos ciclos independientes, incluso con un hijo sano, no son un éxito.
    for prefijo in ("ciclo1", "ciclo2"):
        nodos.extend([n(prefijo + "a", estado=FALLADO, parent_id=prefijo + "b"),
                      n(prefijo + "b", estado=FALLADO, parent_id=prefijo + "a"),
                      n(prefijo + "sano", estado=HECHO, parent_id=prefijo + "a")])
    p = grafo.progreso(nodos)
    assert grafo.sustituidos(nodos) == {"original", "sub"}
    assert p["estado"] == "fallado"
    assert p["fallados"] == 4
    assert p["total"] == 7


# ---------- estado_visible: la etiqueta que ve el humano (6/9/26) ----------
#
# Seis grafos quedaron `activo` en la base 4-5 días parados: la máquina
# tiene razón (siguen siendo relanzables) pero el panel no puede seguir
# diciendo "activo" como si algo estuviera corriendo. `estado_del_grafo`
# NO se toca (lo prueba la sección de arriba); esto es una capa de
# lectura encima.


def test_esperando_humano_sin_nada_corriendo_se_ve_distinto_de_activo():
    """El caso reportado: activo para la máquina, parado para el humano."""
    nodos = [n("a", estado=grafo.ESPERANDO), n("b", "a")]
    assert grafo.estado_del_grafo(nodos) == "activo"        # sin tocar
    assert grafo.estado_visible(nodos) == "esperando_humano"


def test_esperando_humano_con_otra_rama_corriendo_sigue_activo():
    """Si hay algo corriendo DE VERDAD, "activo" no miente — no hay que
    disfrazarlo de espera."""
    nodos = [n("a", estado=grafo.ESPERANDO), n("otro", estado=CORRIENDO)]
    assert grafo.estado_visible(nodos) == "activo"


def test_estado_visible_respeta_un_grafo_cancelado():
    """Cancelar es decisión del humano y no deja rastro en los nodos.

    `estado_del_grafo` solo mira las tareas, así que un grafo cancelado
    con trabajo a medias le da "activo". Medido el 6/9 sobre el grafo
    `demo` cancelado ese día: 7 hechas, 1 fallada, 1 pendiente → decía
    "activo". El panel mostraba corriendo algo que el humano había
    parado a mano.
    """
    nodos = [n("a", estado=HECHO), n("b", estado=FALLADO), n("c")]
    assert grafo.estado_visible(nodos) == "activo"
    assert grafo.estado_visible(nodos, "cancelado") == "cancelado"


def test_estado_visible_no_toca_hecho_ni_fallado():
    assert grafo.estado_visible([n("a", estado=HECHO)]) == "hecho"
    nodos = [n("a", estado=FALLADO), n("b", "a", estado=BLOQUEADO)]
    assert grafo.estado_visible(nodos) == "fallado"


# ---------- es_error_de_presupuesto (6/9/26) ----------
#
# Dos nodos de un grafo de inventorydemo murieron con `budget_exceeded` el 1/9 y
# el grafo quedó `fallado` sin que nada dijera por qué. El texto ya
# está en `tasks.error` (lo escribe `orquestador`); esto solo lo lee.


def test_reconoce_budget_exceeded():
    assert grafo.es_error_de_presupuesto(
        "el nodo terminó en 'budget_exceeded'")


def test_reconoce_budget_split():
    assert grafo.es_error_de_presupuesto("el nodo terminó en 'budget_split'")


def test_un_fallo_comun_no_es_presupuesto():
    assert not grafo.es_error_de_presupuesto("el nodo terminó en 'error'")
    assert not grafo.es_error_de_presupuesto("")
    assert not grafo.es_error_de_presupuesto(None)


# ---------- capas: el grafo dibujable (F3, 2026-08-23) ----------


def test_las_capas_agrupan_lo_que_corre_junto():
    """Estar en la misma fila ES poder correr en paralelo. Eso es lo que
    el dibujo tiene que mostrar y una lista numerada no puede."""
    nodos = [n("a"), n("b"), n("c", "a", "b")]
    assert grafo.capas(nodos) == [["a", "b"], ["c"]]


def test_una_cadena_es_una_capa_por_nodo():
    assert grafo.capas([n("a"), n("b", "a"), n("c", "b")]) \
        == [["a"], ["b"], ["c"]]


def test_se_usa_el_camino_mas_largo_no_el_mas_corto():
    """`d` depende de `a` (temprano) y de `c` (tardío). Con el camino más
    corto subiría a la fila de `b` y su flecha desde `c` iría hacia
    atrás: el dibujo tendría cruces que no significan nada."""
    nodos = [n("a"), n("b", "a"), n("c", "b"), n("d", "a", "c")]
    assert grafo.capas(nodos) == [["a"], ["b"], ["c"], ["d"]]


def test_toda_arista_va_de_una_capa_a_una_posterior():
    """La propiedad que hace dibujable al grafo, sobre uno con varias
    ramas y un reencuentro."""
    nodos = [n("raiz"), n("x", "raiz"), n("y", "raiz"), n("z", "x"),
             n("fin", "y", "z"), n("suelto")]
    filas = grafo.capas(nodos)
    donde = {nid: i for i, fila in enumerate(filas) for nid in fila}
    for nodo in nodos:
        for d in nodo.deps:
            assert donde[d] < donde[nodo.id], f"{d} → {nodo.id} va para atrás"


def test_los_nodos_sueltos_van_a_la_primera_capa():
    assert grafo.capas([n("a"), n("solo")])[0] == ["a", "solo"]


def test_el_mismo_grafo_se_dibuja_siempre_igual():
    """Sin desempate estable la UI baraja los nodos entre refrescos y
    parece que pasó algo."""
    nodos = [n("z", orden=1), n("a", orden=0), n("m", orden=1)]
    assert grafo.capas(nodos) == [["a", "m", "z"]]


def test_un_grafo_con_ciclo_no_tiene_capas():
    with pytest.raises(grafo.GrafoInvalido):
        grafo.capas([n("a", "b"), n("b", "a")])
