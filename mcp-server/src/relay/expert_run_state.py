"""Estado de una corrida compartido por el bucle y su iteración."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExpertRunState:
    """Estado de una corrida; sobrevive a cancelación, steer y auto-continue."""
    _idle_to: float = 0.0
    _inflight: dict = field(default_factory=dict)
    _mcp_tool_to: float = 0.0
    _think_to: float = 0.0
    budget_exceeded: bool = False
    cache_prev: int = 0
    images: list[tuple[bytes, str]] | None = None
    last_phase: str = "thinking"
    last_tool_name: str | None = None
    message_history: list | None = None
    messages_json: str = ""
    meter_turn: int = 0
    out_budget: int = 0
    output_text: str = ""
    progress_events: list[dict[str, Any]] = field(default_factory=list)
    provider_error: BaseException | None = None
    recent_tool_calls: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    request_limit: int = 0
    task_token_limit: int | None = None
    task_usage: Any = None
    rescue: dict | None = None
    spec: str = ""
    steer: list[str] | None = None
    steer_text: str = ""
    t0: float = 0.0
    tokens_in_prev: int = 0
    tokens_out_prev: int = 0
    tool_calls_count: int = 0
    tool_meter: dict[str, dict[str, int]] = field(default_factory=dict)
    usage: Any = None
    user: str = ""
