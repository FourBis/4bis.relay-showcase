"""Check de la propia suite: los tests NO pueden tocar el ~/.4bis real.

Historia (2026-07-31): el fixture de aislamiento vivía en
`tests/conftest_raiz.py` — nombre que pytest no auto-carga — así que
durante meses la suite corrió sin aislamiento y `create_app()` escribió
en el relay.db del usuario (se comprobó: system_config.RELAY_HOST y
FOURBIS_REPOS_ROOT pisados por test_system_config.py).

Este test falla si alguien vuelve a romper el fixture de conftest.py.
"""
from __future__ import annotations

import os
from pathlib import Path

from relay import config


def test_la_suite_no_apunta_al_fourbis_real():
    real = Path.home() / ".4bis"
    for var in ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR"):
        valor = os.environ.get(var)
        assert valor, f"{var} sin setear: el default cae en {real}"
        assert real not in Path(valor).resolve().parents, \
            f"{var}={valor} está dentro del ~/.4bis real"

    # El camino que de verdad usan Database() y create_app().
    assert real not in config.db_path().resolve().parents
