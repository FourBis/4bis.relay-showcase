# Registrar PR externos y evidencia por commit

[English](EXTERNAL_PRS.en.md) · [Issues y Projects](ISSUE_SYNC.md)

**Alcance revisado el 26 de octubre de 2026:** este recorrido está implementado en el código público e integrado en `develop` mediante el [PR #38](https://github.com/FourBis/4bis.relay-showcase/pull/38), del 20 de octubre, con una corrección de sincronización en el [PR #39](https://github.com/FourBis/4bis.relay-showcase/pull/39). La demo y las pruebas documentadas usan datos sintéticos y GitHub simulado. La disponibilidad depende de la versión instalada; esta documentación no acredita un despliegue conectado. Consulta la [evidencia y los límites de esta revisión](qa/showcase-verified-status-2026-10-26.md).

Puedes registrar en **Chat → Registrar PR externo** un PR creado con otra herramienta o manualmente. Relay verifica el repositorio y la identidad en GitHub y conserva el vínculo con la tarea; no genera código ni prepara un worktree para ese registro.

## Recorrido

1. Conecta tu cuenta personal de GitHub en **Mi cuenta**. Necesitas permiso de escritura vigente sobre el proyecto.
2. Elige un proyecto del catálogo autorizado, pega la URL canónica del PR y describe el objetivo. El título de la tarea es independiente del título de GitHub.
3. Declara el riesgo y, opcionalmente, la herramienta utilizada y una issue del mismo repositorio. La herramienta es una declaración; el autor del PR se verifica en GitHub.
4. En **Declarar evidencia**, describe qué probaste y el resultado para el head SHA mostrado. La declaración conserva tu identidad y ese commit.
5. Usa **Actualizar desde GitHub** para volver a verificar estado, head/base, checks y revisiones. El panel conserva las lecturas y declaraciones anteriores.

Si falla una solicitud, el formulario conserva sus datos. Reintentar la misma declaración utiliza el mismo identificador de solicitud. Un registro duplicado se reconcilia con la tarea existente en lugar de crear trabajo paralelo.

## Qué significa cada evidencia

| Dato | Alcance |
| --- | --- |
| Declaración humana | Describe pruebas que una persona afirma haber realizado. Relay no las ejecutó. |
| Check de GitHub | Conserva el resultado informado por GitHub para el SHA correspondiente. |
| Revisión humana | Debe ser independiente del autor y del responsable, y aplicable al head actual. Un bot no sustituye esa revisión. |
| Apto en Relay | La última lectura cumple la política configurada para el riesgo. No bloquea ni autoriza merges en GitHub. |

Un head nuevo vuelve histórica la evidencia del commit anterior. Una revisión revocada, una política retirada o un fallo remoto también pueden retirar la aptitud. La vigencia técnica de la lectura es de cinco minutos: el registro no implica vigilancia continua.

Admin configura la política explícita por riesgo en `projects.defaults_json.external_pr_policy`, mediante la edición del proyecto documentada en la [API](API.md). Sin política válida no se presenta el registro como apto. Registrar no concede permisos de publicación, integración ni despliegue.

El registro externo no ofrece el ejecutor, publicación, seguimiento automático ni diff de un workspace local. El código se modifica con la herramienta elegida y se revisa en GitHub. Un PR fusionado o cerrado puede refrescarse para consultar su resultado sin convertir el registro en una tarea de ejecución.

## Issue compartida y límites

La issue opcional se verifica por identidad y repositorio, y puede compartirse con una tarea de Relay. Vincularla no declara que esté resuelta ni autoriza su cierre. Consulta [Issue común y sincronización](ISSUE_SYNC.md) para la declaración humana y la política opt-in de Projects.

La demo pública usa personas, repositorio, PR y commits ficticios. Sus botones solo cambian un escenario local; no consultan GitHub, no ejecutan pruebas y no representan una instalación conectada. La instalación local utiliza las cuentas y permisos que configures.

Las pruebas de esta función usan datos sintéticos y GitHub simulado. No acreditan reglas de protección, required checks, permisos o movimientos en una cuenta real.
