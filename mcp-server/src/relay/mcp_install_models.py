"""Estados, jobs y rutas persistentes del instalador MCP."""
from __future__ import annotations
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("relay.mcp_installer")

class InstallerState(str, Enum):
    PENDING = "pending"
    CLONING = "cloning"
    CLONED = "cloned"
    SCANNING = "scanning"
    SCANNED = "scanned"
    VETTING = "vetting"
    VETTED = "vetted"
    AWAITING_CONFIRM = "awaiting_confirm"
    INSTALLING = "installing"
    HEALTHY = "healthy"
    HANDSHAKE_FAILED = "handshake_failed"
    INSTALL_FAILED = "install_failed"
    REJECTED = "rejected"
    FAILED = "failed"

@dataclass
class InstallJob:
    id: str
    url: str
    slug: str
    install_dir: str
    state: InstallerState = InstallerState.PENDING
    source_commit: Optional[str] = None
    scan_findings: list[str] = field(default_factory=list)
    vet_verdict: str = "unknown"
    vet_report: str = ""
    error: Optional[str] = None
    # Lo que se va a insertar en mcp_servers cuando confirmas.
    # Detectado de heuristics del repo, pero editable vía confirm.
    proposal: dict = field(default_factory=dict)
    # Override del humano en /confirm: si viene, reemplaza proposal
    # antes de install. Sirve para "el detector se equivocó, ajusta
    # comando/args/env a mano".
    override: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_public(self) -> dict:
        """Lo que la UI consume (no expone campos internos sensibles)."""
        d = asdict(self)
        d["state"] = self.state.value
        return d

def mcp_installs_root() -> Path:
    """Donde se clonan los MCPs durante install. ~/.4bis/mcp-installs/."""
    root = Path(os.environ.get("FOURBIS_MCP_INSTALLS_DIR",
                               str(Path.home() / ".4bis" / "mcp-installs")))
    root.mkdir(parents=True, exist_ok=True)
    return root

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
