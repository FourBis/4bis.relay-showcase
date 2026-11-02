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
| [Segunda corrección, `27fd145`](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41465812512) | 3 fallos, 1.139 aprobados, 1 omitido, 0 errores y 11 subtests aprobados. |
| [Tercera corrección, `dbe2cff`](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41468558707) | Primera tanda: 1.142 aprobados, 1 omitido y 11 subtests. Demo estática aprobada. Segunda tanda: 41 fallos y 263 aprobados. |
| [Cuarta corrección, `b3e9130`](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41473567507) | Primera tanda: 1.142 aprobados, 1 omitido y 11 subtests; demo aprobada; PR externos: 309 aprobados. Orquestación: 4 fallos, 26 aprobados y 79 no seleccionados. |
| [Quinta corrección, `2dd97fb`](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41477755864) | Primera tanda: 1.142 aprobados, 1 omitido y 11 subtests; demo aprobada; PR externos: 340 aprobados; orquestación: 30 aprobados y 79 no seleccionados. Grafos: 14 fallos, 388 aprobados y 5 subtests. |
| [Sexta corrección, `7aecd6e`](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41483169524) | Primera tanda: 1.142 aprobados, 1 omitido y 11 subtests; demo aprobada; PR externos: 340 aprobados; orquestación: 30 aprobados y 79 no seleccionados. Grafos: 1 fallo, 401 aprobados y 5 subtests. |
| [Séptima corrección, `3777e98`](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41486562564) | Primera tanda: 1.142 aprobados, 1 omitido y 11 subtests; demo aprobada; PR externos: 340 aprobados; orquestación: 30 aprobados y 79 no seleccionados; grafos: 402 aprobados y 5 subtests. Etapa final: 9 fallos y 131 aprobados. |
| [Octava corrección, `079adbf`](https://github.com/FourBis/4bis.relay-showcase/actions/runs/41489219264) | Primera tanda, demo, PR externos y orquestación aprobadas. Grafos: 1 fallo, 414 aprobados y 5 subtests; la etapa final quedó omitida. |

En los cinco primeros casos, la primera tanda falló y las etapas siguientes quedaron
omitidas. En el sexto, falló la segunda tanda y las tres últimas quedaron omitidas;
en el séptimo falló orquestación y las dos últimas quedaron omitidas. En el
octavo y el noveno falló grafos y quedó omitida la última etapa de limpieza.
En el décimo, la etapa final falló después de aprobar las anteriores. En el
undécimo falló un caso de grafos y la etapa final quedó omitida.
La diferencia en fallos de archivos bloqueados en Windows no acredita
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

La segunda ejecución dejó tres fallos de preparación de fixtures: dos proyectos
SQL sin el nombre obligatorio y una expectativa HTTP que no representaba el
middleware vigente. El miembro invitado puede leer, pero no controlar el trabajo;
el middleware rechaza ese POST con 404 antes de entrar al handler. La fixture
conserva la comprobación de que no cambian el trabajo ni sus eventos. El contrato
HTTP no se amplía para hacer pasar el caso y el nombre sintético completa la
preparación de los proyectos SQL sin modificar el esquema del producto.

En la siguiente tanda se preparan identidades y permisos persistidos para los
casos de PR externos y se prueba la revocación durante IO contra la base actual.
También se corrige la precedencia que permitía alcanzar el ejecutor de un PR
externo cuando el proyecto no tenía repositorio Git: el rechazo ocurre antes de
la salida anticipada para ese caso. Las listas y búsquedas de PR externos también
exigen acceso vigente al proyecto, como su detalle: al revocar el permiso no
deben devolver sus metadatos. El historial ordinario conserva su propia política.
Estos cambios requieren su propio CI; el run `dbe2cff` sigue fallido.

Una revisión posterior detectó que la autorización para configurar la política
de sincronización podía quedar obsoleta durante una espera. La corrección vuelve
a leer la cuenta en la transacción de guardado y verifica el acceso después de
consultar datos remotos. Las carreras de revocación se cubren con identidades
sintéticas; el siguiente CI debe verificar estas modificaciones.

La tanda de orquestación también evidenció un contador perdido al cancelar una
herramienta ya iniciada: se conserva el mayor conteo observado sin sumar fuentes.
Los casos de interrupción exigen decisión humana y comprueban que no se repita
la ejecución ni se aumente el presupuesto; los textos usan el idioma vigente.

En la tanda de grafos se preparan conversaciones y chats de fixture reales y
se espera a que finalice el trabajo antes de leer el plan. La reanudación ahora
rechaza un proyecto ya deshabilitado antes de inspeccionar su workspace. Sigue
pendiente una carrera de diagnóstico si la autorización cambia durante las
esperas posteriores. La corrección preparada vuelve a comprobar permisos y
versiones en la misma transacción que fusiona el diagnóstico; un cambio de
estado o una revocación impide esa escritura. La lectura Git puede terminar tras
la revocación y las versiones por contenido no detectan un cambio que vuelva
exactamente a su valor anterior. Los casos de carrera se incorporan a la etapa
existente de grafos del CI; su resultado todavía debe comprobarse.

El último fallo observado consultaba las etapas de un chat aún en cola. La
fixture espera ahora un estado terminal con fecha de cierre, sin esperar a que
aparezca el dato que pretende comprobar; mantiene las aserciones de etapas y
su lectura por API. La nueva ejecución debe confirmar el resultado.

El primer CI de la protección atómica detectó que el conflicto 409 no incluía
el identificador del grafo para una tarea detenida. La respuesta conserva ahora
ese identificador en rutas de grafo, junto al código de conflicto; los casos de
concurrencia comprueban ambos campos además de la ausencia de mutación.

Los casos nocturnos preparan su propio estado y worktree privado. Comprueban que
el checkout de origen quede intacto, que los gates restauren cambios conocidos
y que los archivos nuevos no seguidos se conserven para recuperación manual.
El resultado de estos ajustes corresponde al siguiente CI.

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
ni navegador durante esta corrección. El workflow existente añade los casos de
carrera a la etapa de grafos sin quitar comprobaciones. No se modifican
presupuestos, credenciales o servicios para obtener un resultado favorable.
El recorrido visual de la versión corregida y una instalación real permanecen
pendientes de comprobación autorizada.

La generación/edición multimedia nativa y los cambios posteriores de H3/audio/
imágenes/vídeo siguen excluidos. Una imagen adjunta existente o una evidencia
PNG publicada manualmente no incorpora esas funciones.
