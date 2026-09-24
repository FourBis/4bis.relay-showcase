# Equipo y permisos por proyecto

**Equipo** permite registrar integrantes, editar su rol, activar o desactivar
su acceso y asignar proyectos. Cloudflare Access verifica la identidad remota;
Relay exige que esa cuenta esté registrada y activa. El acceso local conserva
el administrador local y los límites descritos en [SECURITY.md](../SECURITY.md).

| Rol | Alcance |
|---|---|
| Admin (`owner`) | Administra el equipo y todos los proyectos; asigna proyectos y controla la publicación de PR. |
| Subadmin (`subadmin`) | Trabaja en proyectos asignados, gestiona integrantes Dev y consulta CRM y consumo; no cambia asignaciones. |
| Dev (`member`) | Consulta el trabajo técnico; edita, compila y prueba solo los proyectos asignados. |
| Finanzas (`finance`) | Consulta CRM, informes y estimaciones de consumo; no accede al código, chats, tools o contactos personales del CRM. Puede conectar su propio Google/Gmail en Mi cuenta. |

## Dar escritura a una persona

1. Como Admin, abre **Equipo** y pulsa **Editar** en la persona.
2. Selecciona sus proyectos y pulsa **Guardar cambios**. Sin asignaciones se
   muestra **Solo lectura**; no se asignan proyectos automáticamente.
3. La persona conecta su propio GitHub desde **Mi cuenta**. El administrador
   configura primero la integración de esa instalación, como indica
   [USER_ACCOUNTS.md](USER_ACCOUNTS.md).
4. Si la tarea ya estaba en solo lectura, la persona abre sus controles y pulsa
   **Habilitar escritura**. Cuando está inactiva, esto prepara su rama y worktree
   y conserva el historial. **Continuar** es una acción posterior y explícita.

La cuenta de GitHub debe tener acceso al repositorio. Si aparece
`Conecta tu cuenta de GitHub en Mi cuenta`, revisa esa conexión aunque el proyecto
ya esté asignado. Rol, asignación y cuenta externa son requisitos separados.

## Límites

La asignación no habilita escritura SQL, herramientas MCP externas ni publicar
o integrar PR. Siempre debe quedar al menos un Admin activo. La comprobación
del último Admin y los cambios de rol/asignación se hacen transaccionalmente.
El backend aplica las restricciones aunque se omita la UI.

Las tareas en cola revalidan la cuenta y sus asignaciones antes de continuar.
Retirar un proyecto o desactivar una persona bloquea ejecuciones posteriores;
no termina a la fuerza un comando que ya está corriendo.

Compilar o probar ejecuta código con la cuenta del servicio Relay. Un worktree
separa archivos, pero no aísla procesos ni credenciales del sistema operativo.
Esta capacidad corresponde a desarrolladores de confianza en una instalación
controlada. Las métricas de costos son estimaciones, no facturas.

La demo pública usa personas y proyectos ficticios. No contiene un roster
operativo ni precarga cuentas de una organización.
