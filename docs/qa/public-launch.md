# Validación de la presentación pública

La web de `site/` es una simulación estática ES/EN. No ejecuta Relay,
proveedores ni herramientas. Sólo este directorio se publica en GitHub Pages.
El artículo y los README describen la aplicación local por separado.

## Reproducir

Con Node y el tooling Playwright usado por las otras comprobaciones visuales:

```powershell
node --check site/demo.js
node docs/qa/public-demo.mjs
```

Por defecto, el check sirve sólo `site/` en un puerto local temporal y cierra
el servidor al terminar. `PUBLIC_DEMO_URL` permite comprobar el mismo recorrido
en la web publicada; `PUBLIC_DEMO_OUT` cambia la carpeta de capturas.

## Comprobaciones

- Dos conversaciones conservan borradores separados al abrir, cerrar y recuperar.
- Renombrar guarda el nombre; Escape cancela; texto escrito se trata como texto.
- Los escenarios conservan la rama, workspace y número de PR ficticios.
- Reinicio de demo, ES/EN, etiquetas accesibles y navegación de pestañas por teclado.
- Anchos de 1440, 390 y 320 píxeles, sin desbordamiento horizontal.
- Imágenes cargadas, sin errores de consola y sin solicitudes externas, fetch,
  XHR ni WebSocket. La CSP bloquea conexiones y scripts de otros orígenes.

La comprobación local pasó el 23 de septiembre de 2026 UTC. Se inspeccionaron
las capturas de escritorio y móvil. Estas pruebas validan la presentación;
no acreditan ejecución de modelos, aislamiento del sistema ni producción.

La demo no conserva los mensajes al recargar. Los enlaces externos abren
GitHub o el sitio público de FourBis sólo al navegar a ellos. No se incorporaron
analítica de visitantes, cookies ni dependencias a la web.
