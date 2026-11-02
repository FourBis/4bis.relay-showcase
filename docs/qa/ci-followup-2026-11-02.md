# Corrección del CI de la ampliación — 2 de noviembre de 2026

La ampliación y su mantenimiento se integraron mediante el
[PR #42](https://github.com/FourBis/4bis.relay-showcase/pull/42), que incluye el
[#41](https://github.com/FourBis/4bis.relay-showcase/pull/41), en el commit
`7e55b75f1fb3be56370923a4ce2b82999c5c9fbb`. Esta rama prepara correcciones sobre
esa integración, conservando sus controles de acceso y seguridad.

## Evidencia de la integración

| Ejecución | Resultado de la primera tanda |
| --- | --- |
| [PR #41](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41374797436) | 12 fallos, 1.105 aprobados, 1 omitido y 11 subtests aprobados. |
| [PR #42](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41375052221) | 70 fallos, 1.056 aprobados, 1 omitido, 8 errores y 11 subtests aprobados. |
| [Commit integrado](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41375783956) | 75 fallos, 1.051 aprobados, 1 omitido, 8 errores y 11 subtests aprobados. |
| [Primera corrección, `e3c30a2`](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41461180731) | 14 fallos, 1.128 aprobados, 1 omitido, 0 errores y 11 subtests aprobados. |

En los cuatro casos, la primera tanda falló y las etapas siguientes quedaron
omitidas. La diferencia en fallos de archivos bloqueados en Windows no acredita
que el contrato de esos endpoints sea distinto entre las ejecuciones.
El [workflow de Pages](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41375783806)
terminó correctamente para la demo. Su éxito no convierte los checks del
servidor en satisfactorios ni acredita una instalación operativa.

## Segunda tanda de corrección

Los fallos restantes del primer candidato separaron tres contratos concretos:
el cliente de la fixture debe cerrarse antes de borrar su directorio temporal;
las consultas SQL del runner necesitan actor y conversación legítimos; y una
invitación de lectura válida debe aparecer en el listado aunque una conversación
histórica tenga creador `NULL`. El cambio del listado conserva su comprobación
de invitación, cuenta vigente y acceso; no concede control a quien sólo lee.

Los cleanups de las fixtures se registran en orden inverso a su ejecución:
cierre de clientes y app antes de borrar los temporales. No se añaden esperas,
reintentos de borrado ni desactivación de workers para ocultar archivos abiertos.
El orden corresponde al [contrato de unittest](https://docs.python.org/3/library/unittest.html#unittest.TestCase.addCleanup).

La lectura preventiva de las etapas todavía omitidas también identificó fixtures
anteriores a los contratos de conocimiento y recuperación. Ahora preparan
fuentes reales con actor/CID, distinguen control del creador e invitaciones de
lectura, y conservan la intención pendiente de verificación con su firma ante
una interrupción. La recuperación comprueba la misma identidad y que no se
repitan nodos ya ejecutados. Estas modificaciones tienen revisión estática;
su resultado en ejecución corresponde a una nueva revisión del PR, no al run
fallido anterior.

## Alcance de la corrección

Se distingue un defecto del producto de un fixture que todavía representa un
contrato anterior. Las correcciones mantienen la identidad actual de cada actor,
las invitaciones explícitas, el rechazo de rutas inválidas, la redacción de
diagnósticos sensibles, los límites de ejecución y la base Git `develop`.
Los módulos que excedían el límite de tamaño se separan por responsabilidad;
no se aumenta el límite ni se omiten sus comprobaciones.

Los fixtures deben representar los recibos, colas, orígenes de conocimiento y
permisos vigentes. Actualizarlos no autoriza a omitir pruebas, suprimir aserciones
negativas o permitir acceso a una conversación por la mera asignación de su
proyecto. Los diagnósticos SQL entregan causas acotadas sin copiar texto libre
del driver, nombres privados o rutas locales.

## Qué se ha comprobado en esta preparación

Se realiza revisión estática del diff, AST, dependencias entre módulos,
equivalencia de los helpers extraídos, sintaxis JavaScript y referencias de
documentación. Estas comprobaciones no importan el producto ni ejecutan sus
fixtures. Las suites se ejecutan únicamente en el CI automático autorizado.
El estado vigente de cada candidato corresponde a los checks de su PR y SHA;
los resultados del commit integrado de arriba siguen siendo evidencia histórica
fallida, aunque una corrección posterior pase.

No se ejecutan suites locales, aplicación, consultas SQLite, modelos, proveedores
ni navegador durante esta corrección. Tampoco se modifican workflows,
presupuestos, credenciales o servicios para obtener un resultado favorable.
El recorrido visual de la versión corregida y una instalación real permanecen
pendientes de comprobación autorizada.

La generación/edición multimedia nativa y los cambios posteriores de H3/audio/
imágenes/vídeo siguen excluidos. Una imagen adjunta existente o una evidencia
PNG publicada manualmente no incorpora esas funciones.
