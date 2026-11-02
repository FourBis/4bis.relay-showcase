# Identidad local y recibos de despliegue

Esta capacidad es opcional y está apagada por defecto. No despliega, detiene,
reinicia ni promueve código por consultar la UI o la salud del proceso.
Activar RELAY_RUNTIME_IDENTITY_ENABLED desde Config requiere reinicio; la
información adicional de salud queda reservada a Admin.

El proceso captura una vez su commit, versión, PID, fecha de inicio y estado
tracked del checkout. Una lectura de salud no asigna números ni crea una base
nueva. Los paths internos del lector no forman parte del payload HTTP.
Si faltan Git, un recibo o una coincidencia verificable, la UI muestra estado
no confirmado o desconocido; no inventa el commit que ejecuta una instancia.
Un recibo confirmado de otro PID no confirma el proceso actual aunque coincidan
commit y versión. Un reinicio no asigna un número nuevo ni transfiere por sí
solo la confirmación del proceso anterior.

El módulo relay.deployment_metadata permite a un operador preparar, confirmar,
marcar fallido y consultar un recibo en una base local elegida explícitamente.
Son operaciones manuales: no se ejecutan al instalar esta rama ni al abrir el
workspace. Un número se asigna sólo al confirmar un intento con los datos
coincidentes y el activo esperado. Un rollback conserva la historia y requiere
un destino confirmado anterior.

El recibo es un registro de la verificación efectuada por el operador y los
controles del programa; no constituye una atestación criptográfica del host.
Los argumentos de confirmación deben provenir del proceso comprobado, no de
una versión supuesta. No hay destinos remotos, tokens ni recursos reales
incluidos en el código de ejemplo.

La revisión de esta rama es estática. No se ejecutó CLI, aplicación, SQLite,
proveedor, despliegue, reinicio ni rollback. Tampoco se verificó una instancia
instalada. El código de preparación y confirmación sólo escribe si el operador
lo invoca de forma explícita en su propio entorno autorizado.
