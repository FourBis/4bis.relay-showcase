# Material de uso para compartir

**Borradores para revisión.** No se han publicado ni enviado desde esta tarea.
Todos los ejemplos visuales usan la demo ficticia del repositorio público.

## Publicación personal

Hoy vi en Relay algo que llevaba tiempo esperando: una tarea larga se convirtió
en un grafo, empezó a avanzar y, cuando una de sus partes agotó su presupuesto
con trabajo pendiente, volvió a subdividirla y continuó.

Lo que me hizo detenerme a mirarlo fue poder seguir el trabajo: qué estaba
terminado, qué dependía de qué, dónde necesitaba intervención y qué había
cambiado en los archivos. La conversación, la rama y el workspace seguían
asociados a la misma tarea.

También estamos haciendo explícitos los permisos del equipo: qué proyectos
puede trabajar cada persona, qué cuenta usa y cuándo habilita escritura.

Ese trabajo todavía estaba en curso al observarlo. Relay sigue siendo un
proyecto experimental, y hoy no tengo una cifra de ahorro que atribuirle.
Sí tengo un comportamiento concreto que puedo enseñar y que me da orgullo
haber construido.

Preparé una demo interactiva con datos ficticios para recorrer esas ideas:
https://fourbis.github.io/4bis.relay-showcase/

¿Cómo sigues el avance de una tarea de IA cuando el plan inicial se queda corto?

## Publicación breve de uso

Una asignación de proyecto, una cuenta de GitHub y una tarea en modo escritura
son tres cosas distintas. En Relay ahora puedes verlas y gestionarlas desde
Equipo, Mi cuenta y los controles de la tarea.

La demo muestra el recorrido: asignar el proyecto, conectar una cuenta ficticia,
habilitar escritura y continuar. También puedes abrir el diff y comprobar que
la conversación conserva su trabajo.

Explora el ejemplo: https://fourbis.github.io/4bis.relay-showcase/

## Guion de video de 75 segundos

| Tiempo | Pantalla y narración |
|---|---|
| 0–10 s | Presentar la demo: «Este ejemplo ficticio muestra cómo Relay organiza una tarea larga». |
| 10–30 s | Mostrar el grafo inicial y avanzar a la subdivisión: «Una tarea llega al presupuesto de herramientas con trabajo pendiente; Relay crea subtareas y actualiza las dependencias». |
| 30–45 s | Equipo: «El Admin asigna proyectos. La cuenta personal y la habilitación de escritura se gestionan por separado». |
| 45–60 s | Continuidad y diff: «Aquí siguen la conversación, los archivos y los cambios; puedo revisarlos antes de continuar». |
| 60–75 s | Mostrar repositorio y cierre: «La demo corre en tu navegador con datos ficticios. El código de Relay está disponible para explorar y probar localmente». |

Mantener visible la etiqueta de simulación y grabar solo el sitio público.
No atribuir al video una migración terminada, ejecución real de IA ni métricas
de ahorro. El [caso de uso](LONG_RUNNING_WORK.md) describe por separado la
observación local y los límites del mecanismo.
