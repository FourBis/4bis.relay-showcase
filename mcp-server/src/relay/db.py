"""Fachada pública de los repositorios SQLite de Relay.

`Database` conserva la API histórica y compone repositorios por dominio.
Cada mixin usa la infraestructura común de conexiones y transacciones; no
hay ORM ni cambios al SQL, locks o migraciones.
"""
from __future__ import annotations

from .db_chats import DatabaseChatsMixin
from .db_conversation_tasks import DatabaseConversationTasksMixin
from .db_conversations import DatabaseConversationsMixin
from .db_core import DatabaseCore
from .db_crm_models import DatabaseCrmModelsMixin
from .db_graph import DatabaseGraphMixin
from .db_knowledge import DatabaseKnowledgeMixin
from .db_metrics import DatabaseMetricsMixin
from .db_night import DatabaseNightMixin
from .db_projects import DatabaseProjectsMixin
from .db_questions import DatabaseQuestionsMixin
from .db_registry import DatabaseRegistryMixin
from .db_schema_init import DatabaseSchemaMixin
from .db_support import (
    MODEL_PRICES_KEY,
    _vence_en,
    cost_usd,
    match_model_price,
    now_iso,
    prices_from_models,
    read_system_config_sync,
)
from .db_schema import FTS_SCHEMA, SCHEMA


class Database(
    DatabaseCore,
    DatabaseSchemaMixin,
    DatabaseRegistryMixin,
    DatabaseProjectsMixin,
    DatabaseCrmModelsMixin,
    DatabaseChatsMixin,
    DatabaseMetricsMixin,
    DatabaseConversationsMixin,
    DatabaseConversationTasksMixin,
    DatabaseKnowledgeMixin,
    DatabaseNightMixin,
    DatabaseGraphMixin,
    DatabaseQuestionsMixin,
):
    """Repositorios de configuración sobre SQLite (métodos async)."""
