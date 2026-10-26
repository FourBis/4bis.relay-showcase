# Estado verificable del showcase — 26 de octubre de 2026

Actualización acotada de contenido y demostración de la edición pública.
Base comprobada: `develop` y `origin/develop` coinciden en
`7c1e55bf4445025143ab901c3da77c101e824989`, sin diferencias locales.
Rama de trabajo: `codex/showcase-verified-status-20261026`, en un checkout
independiente. Esta revisión no publica el sitio ni integra ramas.

## Evidencia y decisiones

| Estado | Evidencia | Límite |
|---|---|---|
| Código público implementado | `external_pr.py`, `issue_links.py`, `issue_sync.py`, el panel `chat-external-pr.js` y las pruebas existentes. | La presencia del módulo no acredita configuración o disponibilidad de una instancia. |
| Integrado en `develop` público | [PR #38](https://github.com/FourBis/4bis.relay-showcase/pull/38), integrado el 20 de octubre, y [PR #39](https://github.com/FourBis/4bis.relay-showcase/pull/39), corrección de observaciones concurrentes. | Una integración de código no demuestra un despliegue. |
| Demo local comprobada | Cinco escenarios sintéticos; PR externo, evidencia por commit, política explícita y resolución completa. | No se consulta GitHub, no se llama a modelos y no se conecta un backend. |
| Instancia desplegada | No se verificó un despliegue conectado en esta revisión. | Comprobar versión instalada, permisos, configuración y recorrido real antes de afirmar disponibilidad. |

Se revisaron `AGENTS.md`, `CONTRIBUTING.md`, `.github/workflows/checks.yml`
y `.agents/skills/relay-ui/SKILL.md`. La skill local corresponde al panel
admin; aquí se conserva el CSS y no se modifica ese panel.

La consulta de issues y PR abiertos del repositorio público no devolvió
resultados. La colección completa revisada contiene PR cerrados, sin issues
de trabajo que actualizar o cerrar. Se conservan como fuentes los PR #38 y
#39; no se duplica su implementación ni se crea una nueva issue o PR.

## Cambios

- `README.md` y `README.en.md`: estado de implementación, integración,
  simulación y disponibilidad, con fuentes públicas.
- `docs/CAPABILITY_MAP.md`, `docs/EXTERNAL_PRS*.md` y `docs/ISSUE_SYNC*.md`:
  alcance fechado sin sustituir la evidencia histórica por una validación global.
- `site/index.html`: avisos de alcance ES/EN, evaluación identificada como
  simulación y referencia de instalación alineada con `SETUP.md`: Python 3.15
  recomendado, mínimo 3.11 según `pyproject.toml`.
- `docs/VALIDATION.md`: enlace a este registro de revisión.

Se mantienen el diseño y los cinco escenarios existentes. No se incorporan
funciones nuevas del servidor, código de otras ediciones, resultados de
proveedores, métricas ni datos reales.

## Validación local

Entorno de esta revisión: Windows, Node.js `v24.21.0` y Python `3.12.0`.
No se atribuyen estos resultados al entorno de CI, que configura Python 3.15
y Node.js 22.

| Comprobación | Resultado |
|---|---|
| `node --check site/demo.js` | Aprobado. |
| `node --check site/tour.js` | Aprobado. |
| `node site/external-demo.test.mjs` | Aprobado: evidencia por SHA, cierre explícito y límites offline. |
| Recorrido Playwright sobre `site/` servido solo en loopback | Aprobado: cinco pestañas ES/EN, reset, Home/End y navegación circular por teclado. |
| Recorrido de PR externos | Aprobado: exige declaración, checks y revisión; un head nuevo vuelve histórica la evidencia; los merges solos no cierran la issue; la política tampoco sustituye la declaración completa. |
| Geometría y capturas | ES/EN a 1440, 390 y 320 px, sin desbordamiento de página; aviso de disponibilidad contenido. Inspección visual de escritorio ES, móvil EN de 320 px y aviso de alcance EN. |
| Consola y red del recorrido | Sin errores de página/consola. Todas las solicitudes observadas se limitaron al servidor estático local. |
| Referencias locales | 231 enlaces Markdown revisados y recursos locales del HTML con destino existente. |
| `git diff --check` | Aprobado. |

Las capturas y el script de navegación se conservan como evidencia local
fuera del árbol publicable, en `validation-artifacts/playwright` junto al
checkout. No son capturas de una instancia de Relay conectada.

El primer inicio de Chrome dentro del sandbox falló al crear su proceso de
crash reporting. La sesión aislada iniciada fuera de ese sandbox cargó la
demo desde un servidor estático local y completó el recorrido. Una captura
aislada inicial agotó su tiempo de espera; el recorrido completo posterior
generó las seis capturas de idiomas/tamaños y el aviso de alcance.

Se intentó además:

```powershell
python -m pytest mcp-server/tests/test_external_pr.py mcp-server/tests/test_issue_sync.py mcp-server/tests/test_issue_workflow_integration.py -q
```

No se recopilaron pruebas: `conftest.py` no pudo importar `pydantic_ai.models`
(`ModuleNotFoundError: No module named 'pydantic_ai'`). Este intérprete no
tiene instaladas las dependencias de desarrollo del servidor. La suite
Python queda pendiente en un entorno con `mcp-server[dev]`; no se declara
aprobada ni se reemplaza por resultados históricos. No se cambió el backend.

## Pendientes de aprobación o comprobación

- Revisar estos cambios locales antes de autorizar su publicación o PR.
- Reproducir la suite Python adicional en el entorno de desarrollo adecuado
  si se requiere esa evidencia complementaria.
- Confirmar por separado versión y disponibilidad de cada integración en la
  instancia donde se vaya a usar. Esta revisión no autoriza ni ejecuta una
  promoción, publicación o despliegue.
