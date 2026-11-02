"""Current, explicit conversation access. Project membership alone never shares a chat."""
from __future__ import annotations

import json

from . import identity


async def transaction(db, operation, *, write=False):
    def work():
        conn = db._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            result = operation(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    return await db._in_thread(work)


def user_tx(conn, actor):
    actor = str(actor or "").strip().lower()
    if actor == identity.OWNER:
        return {"email": actor, "role": "owner", "enabled": True, "project_slugs": []}
    row = conn.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (actor,)).fetchone()
    user = dict(row) if row else {}
    if not user.get("enabled") or user.get("archived_at") or user.get("role") not in {"owner", "member", "subadmin"}:
        return {}
    try:
        grants = json.loads(user.get("project_slugs_json") or "[]")
    except (ValueError, TypeError):
        grants = []
    # Match the public user API: a string/object is never a list of grants.
    user["project_slugs"] = grants if isinstance(grants, list) else []
    return user


def access_tx(conn, conversation_id, actor):
    actor = str(actor or "").strip().lower()
    denied = dict(can_read=False, can_collaborate=False, can_control=False, can_send=False,
                  can_share=False, is_creator=False, relation=None, share_mode=None, project_assigned=False)
    user = user_tx(conn, actor)
    row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if not row or not user:
        return denied
    conv = dict(row)
    project = conn.execute("SELECT * FROM projects WHERE slug=? COLLATE NOCASE", (conv["project_slug"],)).fetchone()
    project = dict(project) if project else {}
    try:
        task = json.loads(conv.get("task_json") or "{}")
        defaults = json.loads(project.get("defaults_json") or "{}")
    except (ValueError, TypeError):
        return denied
    if not isinstance(task, dict) or not isinstance(defaults, dict):
        return denied
    creator = str(conv.get("requested_by") or "").strip().lower() == actor
    admin = user["role"] == "owner"
    assigned = bool(project.get("enabled") and identity.user_can_write_project(user, conv["project_slug"]))
    share = conn.execute("SELECT permission FROM conversation_shares WHERE conversation_id=? "
                         "AND email=? COLLATE NOCASE AND revoked_at IS NULL", (conversation_id, actor)).fetchone()
    mode = share["permission"] if share and assigned else None
    read = creator or admin or mode in {"read", "collaborate"}
    if task.get("mcp_private"):
        # The trusted local adapter is not a personally authenticated MCP user.
        from .private_mcp_access import allowed_project_tx
        read = bool(actor != identity.OWNER and assigned and (creator or admin)
                    and allowed_project_tx(conn, user, {**project, "defaults_json": defaults}))
        mode = None  # Private MCP conversations cannot be shared.
    writable = bool(assigned and not defaults.get("read_only") and not task.get("read_only")
                    and task.get("mode") != "read_only" and not task.get("mcp_private"))
    collaborate = bool(read and writable and (creator or admin or mode == "collaborate"))
    control = bool(read and (creator or admin))
    return dict(can_read=bool(read), can_collaborate=collaborate, can_control=control,
                can_send=bool(project.get("enabled") and (control or collaborate)),
                can_share=bool(read and (admin or creator and assigned) and not task.get("mcp_private")),
                is_creator=creator, relation="owner" if creator else "admin" if admin else "shared" if mode else None,
                share_mode=mode, project_assigned=assigned)


def require_tx(conn, conversation_id, actor, permission="read"):
    if permission not in {"read", "collaborate", "control", "send", "share"}:
        raise ValueError("Unknown conversation permission")
    access = access_tx(conn, conversation_id, actor)
    if not access["can_" + permission]:
        raise PermissionError("Conversación no disponible para esta cuenta")
    return access


async def get_access(db, conversation_id, actor):
    return await transaction(db, lambda conn: access_tx(conn, conversation_id, actor))


async def require(db, conversation_id, actor, permission="read"):
    return await transaction(db, lambda conn: require_tx(conn, conversation_id, actor, permission))


async def current_user(db, actor):
    return await transaction(db, lambda conn: user_tx(conn, actor))


def visibility_tx(conn, conversation_id, actor, permission="read"):
    """One current account/ACL snapshot for response projection."""
    user = user_tx(conn, actor)
    access = require_tx(conn, conversation_id, actor, permission)
    return {"user": user, "access": access, "is_owner": user.get("role") == "owner"}


async def current_visibility(db, conversation_id, actor, permission="read"):
    return await transaction(db, lambda conn: visibility_tx(conn, conversation_id, actor, permission))


def project_conversation_tx(conn, row, actor):
    """Share conversation content, never the owner's operational task config."""
    from .task_service import visible_task
    view = dict(row)
    visibility = visibility_tx(conn, view["id"], actor)
    if "task_json" in view:
        task = visible_task(view["task_json"], owner=visibility["is_owner"])
        view["task_json"] = json.dumps(task, ensure_ascii=False)
    view["access"] = visibility["access"]
    return view


async def can_read_chat(db, chat_id, actor):
    def work(conn):
        user = user_tx(conn, actor)
        chat = conn.execute("SELECT conversation_id,requested_by FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not user or not chat:
            return False
        if chat["conversation_id"]:
            return access_tx(conn, chat["conversation_id"], actor)["can_read"]
        return user["role"] == "owner" or str(chat["requested_by"] or "").lower() == str(actor).lower()
    return await transaction(db, work)


def share_view_tx(conn, cid, actor):
    access = require_tx(conn, cid, actor, "share")
    conv = conn.execute("SELECT project_slug,requested_by,task_json FROM conversations WHERE id=?", (cid,)).fetchone()
    project = conn.execute("SELECT enabled,defaults_json FROM projects WHERE slug=? COLLATE NOCASE", (conv["project_slug"],)).fetchone()
    task = json.loads(conv["task_json"] or "{}")
    defaults = json.loads(project["defaults_json"] or "{}") if project else {}
    writable = bool(project and project["enabled"] and not defaults.get("read_only") and not task.get("read_only")
                    and task.get("mode") != "read_only")
    recipients = []
    for row in conn.execute("SELECT email,display_name FROM users ORDER BY display_name,email"):
        user = user_tx(conn, row["email"])
        if (user and user["role"] in {"owner", "member", "subadmin"}
                and user["email"].lower() != str(conv["requested_by"] or "").lower()
                and project and project["enabled"] and identity.user_can_write_project(user, conv["project_slug"])):
            recipients.append({"email": user["email"], "display_name": row["display_name"] or "",
                               "can_collaborate": writable})
    shares = [dict(row) for row in conn.execute(
        "SELECT s.email,COALESCE(u.display_name,'') AS display_name,s.permission FROM conversation_shares s "
        "LEFT JOIN users u ON u.email=s.email COLLATE NOCASE WHERE s.conversation_id=? AND s.revoked_at IS NULL ORDER BY s.email", (cid,))]
    return {"shares": shares, "recipients": recipients, "access": access}


async def share_view(db, cid, actor):
    return await transaction(db, lambda conn: share_view_tx(conn, cid, actor))


async def set_share(db, cid, actor, email, permission):
    def work(conn):
        view = share_view_tx(conn, cid, actor)
        target = next((row for row in view["recipients"] if row["email"].lower() == email.lower()), None)
        if not target or (permission == "collaborate" and not target["can_collaborate"]):
            raise ValueError("El integrante no tiene acceso vigente al proyecto o la conversación no permite colaborar")
        conn.execute("INSERT INTO conversation_shares(conversation_id,email,permission,created_by,created_at,updated_at) "
                     "VALUES(?,?,?,?,datetime('now'),datetime('now')) ON CONFLICT(conversation_id,email) DO UPDATE SET "
                     "permission=excluded.permission,created_by=excluded.created_by,updated_at=datetime('now'),revoked_at=NULL",
                     (cid, target["email"].lower(), permission, actor))
        return share_view_tx(conn, cid, actor)
    return await transaction(db, work, write=True)


async def revoke_share(db, cid, actor, email):
    def work(conn):
        require_tx(conn, cid, actor, "share")
        conn.execute("UPDATE conversation_shares SET revoked_at=datetime('now'),updated_at=datetime('now') "
                     "WHERE conversation_id=? AND email=? COLLATE NOCASE AND revoked_at IS NULL", (cid, email))
        return share_view_tx(conn, cid, actor)
    return await transaction(db, work, write=True)


async def listing(db, actor, *, scope="mine", project=None, status=None, limit=100):
    def work(conn):
        user = user_tx(conn, actor)
        if not user or (scope == "admin" and user.get("role") != "owner"):
            raise PermissionError("No tienes acceso a esta lista")
        where, params = [], []
        if scope == "mine":
            where.append("c.requested_by=? COLLATE NOCASE")
            params.append(actor)
        elif scope == "shared":
            where.append("COALESCE(c.requested_by,'')<>? COLLATE NOCASE AND EXISTS (SELECT 1 FROM conversation_shares s "
                         "WHERE s.conversation_id=c.id AND s.email=? COLLATE NOCASE AND s.revoked_at IS NULL)")
            params.extend([actor, actor])
        elif scope != "admin":
            raise ValueError("Vista de conversaciones inválida")
        if project:
            where.append("c.project_slug=? COLLATE NOCASE")
            params.append(project)
        if status:
            where.append("c.status=?")
            params.append(status)
        cols = ("c.id,c.project_slug,c.discord_thread_id,c.status,c.started_at,c.closed_at,c.last_activity_at,"
                "c.summary,c.branch,c.pr_url,c.requested_by,c.task_json,"
                "CASE WHEN json_valid(c.messages_json) THEN json_array_length(c.messages_json) ELSE 0 END AS messages_len,"
                "COALESCE(u.display_name,'') AS owner_display_name,"
                "(SELECT substr(h.user_prompt,1,160) FROM chats h WHERE h.conversation_id=c.id ORDER BY h.started_at,h.id LIMIT 1) AS preview")
        rows = conn.execute("SELECT " + cols + " FROM conversations c LEFT JOIN users u ON u.email=c.requested_by COLLATE NOCASE "
                            + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY c.last_activity_at DESC,c.id", params)
        result = []
        for row in rows:
            access = access_tx(conn, row["id"], actor)
            if access["can_read"]:
                result.append({**project_conversation_tx(conn, row, actor),
                               "owner_email": row["requested_by"]})
                if len(result) >= limit:
                    break
        return result
    return await transaction(db, work)


async def can_read_attachment(db, actor, attach_id):
    from . import attachments
    blob = attachments.resolve(attach_id)
    if blob is None or blob.is_symlink() or blob.resolve().parent != attachments.attachments_dir().resolve():
        return False
    def work(conn):
        user = user_tx(conn, actor)
        if not user:
            return False
        if user["role"] == "owner":
            return True  # Preserve administrative reads, using the current DB role, not its cache.
        if conn.execute("SELECT 1 FROM attachment_uploaders WHERE attachment_id=? AND actor=? COLLATE NOCASE",
                        (attach_id, actor)).fetchone():
            return True
        candidates = conn.execute("SELECT c.id FROM conversations c WHERE c.requested_by=? COLLATE NOCASE OR "
            "EXISTS(SELECT 1 FROM conversation_shares s WHERE s.conversation_id=c.id AND s.email=? COLLATE NOCASE AND s.revoked_at IS NULL)", (actor, actor))
        for row in candidates:
            scope = attachments.scope_dir(row["id"]).resolve()
            link = scope / blob.name
            if (access_tx(conn, row["id"], actor)["can_read"] and link.is_file()
                    and not link.is_symlink() and link.resolve().parent == scope):
                return True
        return False
    return await transaction(db, work)
