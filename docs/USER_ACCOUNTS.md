# Cuentas personales

**Mi cuenta** conecta GitHub y Google/Gmail con la identidad verificada de la
persona en Relay. La instalación debe configurar sus propios clientes OAuth;
el repositorio público no incluye aplicaciones registradas, secretos ni cuentas.

## Configurar la instalación

El administrador configura estos valores fuera de Git:

```text
RELAY_PUBLIC_URL=https://relay.example.test
RELAY_GITHUB_CLIENT_ID=<client-id>
RELAY_GITHUB_CLIENT_SECRET=<client-secret>
RELAY_GOOGLE_CLIENT_ID=<client-id>
RELAY_GOOGLE_CLIENT_SECRET=<client-secret>
RELAY_OAUTH_KEY=<fernet-key>
```

El dominio es ilustrativo. Usa la URL HTTPS real de tu instalación y registra
estos callbacks con los proveedores correspondientes:

```text
https://relay.example.test/admin/api/account/github/callback
https://relay.example.test/admin/api/account/google/callback
```

GitHub usa OAuth de usuario de una GitHub App. Los permisos de la App y del
usuario deben cubrir las operaciones que autorices: contenido y PR para
trabajo con Git, issues para su consulta/gestión y checks/statuses para CI.
Los permisos de Relay siguen aplicándose aunque GitHub permita más operaciones.

Google usa OAuth web con identidad, lectura de Gmail y envío de correo:
`openid`, `email`, `gmail.readonly` y `gmail.send`. Configura el consentimiento
y Gmail API en tu propio proyecto. No se usa delegación de todo el dominio.

`RELAY_OAUTH_KEY` es una clave Fernet estable que cifra los tokens en la base
local. Guárdala fuera de Git y de la base, y respáldala de manera segura. Rotarla
sin migrar los datos impide recuperar las conexiones guardadas.
Relay lee la configuración OAuth del proceso o de `HKCU\Environment` en Windows,
la conserva en memoria y retira los secretos del entorno heredable por sus hijos.

## Identidad y acciones

Cada persona conecta su cuenta desde **Mi cuenta**. Configurar un cliente OAuth
no concede acceso a una cuenta ni a un buzón. La identidad externa no se deduce
del correo de Equipo.

Las operaciones autenticadas usan al actor del turno o evento. GitHub, Git y
`gh` no recurren a la cuenta del administrador cuando falta la conexión personal.
Los commits usan el autor noreply verificado por GitHub. Los comandos de shell
del chat no reciben los tokens personales: las operaciones autenticadas pasan
por las herramientas y controles de Relay.

La lectura solicitada de Gmail puede compartir su resultado en el hilo. Preparar
un mensaje crea un borrador temporal por una hora; la persona revisa y pulsa
**Enviar** desde **Mi cuenta**. Un timeout deja el envío incierto y no lo reintenta
automáticamente. Desconectar elimina el token local; la revocación adicional
en el proveedor corresponde a la persona.

## Evidencia

Los tests de cuentas, actor y correo usan proveedores simulados. La demo estática
no inicia OAuth ni envía mensajes. Una instalación debe comprobar su propio flujo
con usuarios autorizados antes de afirmar que las integraciones funcionan allí.
Consulta [Equipo](TEAM_ACCESS.md) y [SECURITY.md](../SECURITY.md) para el alcance.
