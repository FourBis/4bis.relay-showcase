# Cuentas personales

**Mi cuenta** conecta GitHub y Google/Gmail con la identidad verificada de la
persona en Relay. La instalación debe configurar sus propios clientes OAuth;
el repositorio público no incluye aplicaciones registradas, secretos ni cuentas.

## Configurar la instalación

### Primera instalación con GitHub

Con una base sin usuarios, `/admin/` ofrece el alta del primer Admin desde
localhost, sin proxy. El asistente usa una **OAuth App** propia de GitHub;
consulta el [paso a paso](SETUP.md#crear-el-primer-admin-con-github).
El login solicita `read:user user:email`, verifica el correo principal y vincula
la cuenta al identificador estable de GitHub. Otra cuenta con el mismo correo
no puede sustituir esa identidad. El token del login se descarta tras verificarla.

La sesión dura 12 horas, se guarda como hash en SQLite y se envía en una cookie
HttpOnly, SameSite=Lax y Secure cuando se usa HTTPS. **Cerrar sesión** revoca
la sesión del navegador; desactivar al usuario impide que sus sesiones accedan.
Reiniciar Relay conserva las sesiones vigentes y la configuración.

El asistente guarda sus credenciales y una clave de cifrado en un archivo
`.oauth.json` junto a la base, por ejemplo `relay.db` → `relay.oauth.json`.
Ese archivo tiene prioridad sobre el entorno y queda excluido de Git; protégelo
y respáldalo con las mismas precauciones que la base. El archivo no se devuelve
al navegador. HTTP solo se acepta para `localhost`, `127.0.0.1` y `[::1]`.

El alta inicial se cierra al existir cualquier usuario. No migra instalaciones
anteriores ni permite autorregistro de integrantes posteriores: estos conservan
el acceso verificado existente descrito en [Equipo](TEAM_ACCESS.md).

### Cuentas de trabajo y configuración existente

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

GitHub admite OAuth de usuario de una GitHub App. Los permisos de la App y del
usuario deben cubrir las operaciones que autorices: contenido y PR para
trabajo con Git, issues para su consulta/gestión y checks/statuses para CI.
Los permisos de Relay siguen aplicándose aunque GitHub permita más operaciones.

En instalaciones creadas con el asistente, **Conectar GitHub** solicita por
separado `repo read:user user:email` a la OAuth App. Revisa el alcance en GitHub
antes de autorizarlo: `repo` cubre repositorios públicos y privados accesibles
para esa cuenta. La configuración manual equivalente es `RELAY_GITHUB_SCOPES`;
déjala vacía al usar una GitHub App, que define sus permisos en la propia App.
Referencia: [scopes de OAuth Apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps).

Google usa OAuth web con identidad, lectura de Gmail y envío de correo:
`openid`, `email`, `gmail.readonly` y `gmail.send`. Configura el consentimiento
y Gmail API en tu propio proyecto. No se usa delegación de todo el dominio.

`RELAY_OAUTH_KEY` es una clave Fernet estable que cifra los tokens en la base
local. Guárdala fuera de Git y de la base, y respáldala de manera segura. Rotarla
sin migrar los datos impide recuperar las conexiones guardadas.
Sin el archivo local del asistente, Relay lee OAuth del proceso o de `HKCU\Environment` en Windows,
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
