"""Parsers de la salida de texto de `cbm cli` (admin.py).

Regresión 2026-07-25: cbm imprime tablas legibles, no JSON. _cbm_cli buscaba
"la última línea que arranca con { o [" y devolvía {} contra search_graph /
get_architecture — por eso /index/files respondía files:[] siempre.
"""
from relay.admin import _parse_cbm_search_graph, _parse_cbm_sections, _split_row

# Capturado de `cbm cli get_architecture` 0.8.x contra SampleApp.
ARCH_TEXT = """project: C-Users-demo-source-repos-AuroraDemo-SampleApp
total_nodes: 11430
total_edges: 35793
boundaries: 3  (cols: from to calls)
  Services Repositories 376
  Controllers Services 352
  BackgroundJobs Services 11
layers: 3  (cols: name layer reason)
  - api "has HTTP route definitions"
  Controllers entry "only outbound calls"
  Helpers core "high fan-in (111 in, 0 out)"
hotspots: 2  (cols: qn fan_in)
  C-Users-x.Shared.RetornoVM.RetornoVM 283
  C-Users-x.Infra.EfUnitOfWork.SaveChangesAsync 87
clusters: 1  (cols: id label members cohesion top_nodes packages edge_types)
  4 Web 355 0.7712 RetornoVM;MapToVM Service;Web CALLS
routes: 2  (cols: method path handler)
  POST /appointments -
  - /patientfiles/:fileId -
"""

SEARCH_TEXT = """total: 633
results: 3  (rows: name label lines in out; qn = group prefix + "." + name)
C-Users-demo-SampleApp..env (.env.example):
  __file__ File  0 0
C-Users-demo-SampleApp.Web.react-app (Web/react-app/src/services/api.ts):
  __file__ File  0 0
has_more: true
"""


def test_sections_scalars_and_tables():
    d = _parse_cbm_sections(ARCH_TEXT)
    assert d["total_nodes"] == 11430
    assert d["project"] == "C-Users-demo-source-repos-AuroraDemo-SampleApp"
    assert d["boundaries"][0] == {"from": "Services", "to": "Repositories",
                                  "calls": "376"}
    assert len(d["boundaries"]) == 3


def test_sections_quoted_field_keeps_spaces():
    d = _parse_cbm_sections(ARCH_TEXT)
    helpers = [r for r in d["layers"] if r["name"] == "Helpers"][0]
    assert helpers["layer"] == "core"
    assert helpers["reason"] == "high fan-in (111 in, 0 out)"


def test_sections_hotspots_clusters_routes():
    d = _parse_cbm_sections(ARCH_TEXT)
    assert d["hotspots"][0]["fan_in"] == "283"
    c = d["clusters"][0]
    assert c["members"] == "355" and c["cohesion"] == "0.7712"
    assert c["top_nodes"] == "RetornoVM;MapToVM"
    assert d["routes"][0] == {"method": "POST", "path": "/appointments",
                              "handler": "-"}


def test_split_row_respects_quotes():
    assert _split_row('Helpers core "a b c"', 3) == ["Helpers", "core", "a b c"]
    assert _split_row("a b 3", 3) == ["a", "b", "3"]


def test_search_graph_extracts_paths_from_group_headers():
    files, total, has_more = _parse_cbm_search_graph(SEARCH_TEXT)
    assert total == 633
    assert has_more is True
    assert [f["path"] for f in files] == [
        ".env.example", "Web/react-app/src/services/api.ts"]
    assert files[1]["name"] == "api.ts"
    assert files[1]["extension"] == ".ts"


def test_parsers_survive_empty_and_garbage():
    assert _parse_cbm_sections("") == {}
    assert _parse_cbm_search_graph("") == ([], 0, False)
    # Filas huérfanas (sin cabecera) no explotan.
    assert _parse_cbm_sections("  huerfana 1 2") == {}
