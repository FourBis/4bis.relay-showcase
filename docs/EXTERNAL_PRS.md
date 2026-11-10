# Registrar PR externos y evidencia por commit

[English](EXTERNAL_PRS.en.md) · [Issues y Projects](ISSUE_SYNC.md)

**Alcance revisado el 26 de octubre de 2026:** este recorrido está implementado en el código público e integrado en `develop` mediante el [PR #38](https://github.com/FourBis/4bis.relay-showcase/pull/38), del 20 de octubre, con una corrección de sincronización en el [PR #39](https://github.com/FourBis/4bis.relay-showcase/pull/39). La demo y las pruebas documentadas usan datos sintéticos y GitHub simulado. La disponibilidad depende de la versión instalada; esta documentación no acredita un despliegue conectado. Consulta la [evidencia y los límites de esta revisión](qa/showcase-verified-status-2026-10-26.md).

Puedes registrar en **Chat → Registrar PR externo** un PR creado con otra herramienta o manualmente. Relay verifica el repositorio y la identidad en GitHub y conserva el vínculo con la tarea; no genera código ni prepara un worktree para ese registro.

## Recorrido

1. Conecta tu cuenta personal de GitHub en **Mi cuenta**. Necesitas permiso de escritura vigente sobre el proyecto.
2. Elige un proyecto del catálogo autorizado. El formulario tiene un solo campo: pega la URL del PR o búscalo por `#número` o por palabras del título. Al elegirlo ves una ficha leída de GitHub con título, descripción, ramas, totales y commits; no escribes nada a mano. Si el PR no tiene descripción, la ficha lo advierte, porque es el contexto de la revisión.
3. Confirma con **Registrar PR**. El título del registro es el del PR y se aplica la política de riesgo `medium`. Si el PR ya está registrado en ese proyecto, el botón abre su ficha sin volver a registrarlo. La API sigue aceptando `title`, `risk`, `issue` y `actor_label` opcionales; la herramienta es una declaración y el autor del PR se verifica en GitHub.
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

Un head nuevo vuelve histórica la evidencia del commit anterior. Una revisión revocada, una política retirada o un fallo remoto también pueden retirar la aptitud. La vigencia técnica de la lectura es de cinco minutos. Registrar una PR por sí solo no habilita observación automática.

Admin configura la política explícita por riesgo en `projects.defaults_json.external_pr_policy`, mediante la edición del proyecto documentada en la [API](API.md). Sin política válida no se presenta el registro como apto. Como el formulario rápido registra con el riesgo `medium`, esa es la entrada que conviene configurar primero. Registrar no concede permisos de publicación, integración ni despliegue.

El registro externo no ejecuta cambios ni prepara un diff de workspace local.
El código se modifica con la herramienta elegida y se revisa en GitHub. Un PR
fusionado o cerrado puede refrescarse para consultar su resultado sin convertir
el registro en una tarea de ejecución. El candidato local de paridad descrito
abajo añade revisión informativa y publicación configurables por separado.

## Issue compartida y límites

La issue opcional se verifica por identidad y repositorio, y puede compartirse con una tarea de Relay. Vincularla no declara que esté resuelta ni autoriza su cierre. Consulta [Issue común y sincronización](ISSUE_SYNC.md) para la declaración humana y la política opt-in de Projects.

La demo pública usa personas, repositorio, PR y commits ficticios. Sus botones solo cambian un escenario local; no consultan GitHub, no ejecutan pruebas y no representan una instalación conectada. La instalación local utiliza las cuentas y permisos que configures.

Las pruebas de esta función usan datos sintéticos y GitHub simulado. No acreditan reglas de protección, required checks, permisos o movimientos en una cuenta real.

## Candidato local de paridad: revisión y evidencia visual

**Estado al 1 de noviembre de 2026:** estas ampliaciones están preparadas en
una rama local independiente. Esta documentación no afirma publicación remota,
integración, despliegue ni activación de la funcionalidad. La revisión de esta
tanda fue estática: no se ejecutaron app, DB, modelos, proveedores, unitarias ni
suites. La evidencia de octubre citada arriba corresponde a aquella versión.

El candidato conserva la comprobación de base y head en el mismo repositorio,
el rechazo de forks y la lectura fresca del remote. Añade registro por proyecto,
selectores paginados de PR/issues y un informe automático ligado a head, base,
objetivo, issue, evidencia y directrices verificadas del commit base.

La revisión comienza desactivada y requiere que un Admin elija explícitamente
un modelo habilitado y active la política del proyecto. No ejecuta código ni
pruebas. Sus hallazgos son asesoría y se muestran separados de la revisión
humana independiente. Los intentos interrumpidos o inválidos requieren un
reintento explícito, conservando su procedencia.

Publicar comentarios de GitHub es otra opción, apagada inicialmente. Requiere
modo e identidad de cuenta verificados; una respuesta incierta se reconcilia
sin repetir automáticamente el POST. Los avisos son una bandeja local para
cuentas autorizadas configuradas de forma explícita, sin envíos a otras apps.

La evidencia PNG permite preparar una copia temporal propia, descargarla y
subirla manualmente mediante la interfaz de GitHub. Relay verifica después el
enlace, autor, destino, marcador y hash antes de limpiar su copia. No genera ni
sube la imagen, no la envía al modelo y no la considera una prueba ejecutada
ni una aprobación humana.

Consulta [Revisión externa, estados y permisos](EXTERNAL_REVIEW.md) para el
contrato y sus límites. [Issues y Projects](ISSUE_SYNC.md) conserva su política
separada, con protección frente a observaciones concurrentes; una evidencia o
revisión informativa no autoriza cerrar la issue.
