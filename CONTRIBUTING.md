# Contribuir

FourBis Relay se publica como proyecto de portafolio. Las contribuciones deben conservar el foco en un workspace local, conversacional y multi-repositorio, y deben poder revisarse desde el código y la documentación del repositorio.

## Flujo

1. Parte de `develop` y crea una rama descriptiva con prefijo `codex/` cuando corresponda.
2. Mantén los cambios acotados al problema que motivó el pull request.
3. Actualiza la documentación afectada y explica cualquier cambio de comportamiento.
4. Ejecuta las verificaciones relevantes desde la raíz y reporta el comando y su resultado real.
5. Abre el pull request contra `develop`. `master` recibe cambios únicamente mediante el flujo de revisión establecido.

## Reglas de publicación

- No subas claves, tokens, credenciales, bases locales, logs, archivos `.env` ni rutas personales.
- Usa placeholders vacíos en ejemplos de configuración.
- No incluyas nombres, datos, URLs o referencias identificables de clientes y sistemas privados.
- No presentes capturas, badges, métricas, despliegues o resultados de pruebas que no estén comprobados.
- Respeta la atribución a FourBis y Jeremías Badilla.
- El código propio se distribuye bajo MIT; conserva los avisos de copyright y licencia en copias o partes sustanciales.
- No atribuyas la licencia MIT a dependencias de terceros: sus licencias y avisos se mantienen por separado.

## Código y documentación

Reutiliza los patrones existentes y evita dependencias nuevas para problemas que ya resuelven Python, PowerShell o las dependencias declaradas. Los cambios que afectan la interfaz deben mantener la información de [docs/ADMIN_UI.md](docs/ADMIN_UI.md) y [docs/UI_WORKSPACE.md](docs/UI_WORKSPACE.md) consistente con el comportamiento real.

Antes de enviar un pull request, revisa el diff, busca secretos y confirma que los enlaces de la documentación apunten a archivos existentes. Si una validación no pudo ejecutarse, déjalo indicado en el pull request.
