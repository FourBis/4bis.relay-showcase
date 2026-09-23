"""Fachada compatible del pipeline de instalación de MCPs."""
from __future__ import annotations

from . import mcp_install_models, mcp_install_orchestrator, mcp_install_scan

from .mcp_install_models import (  # noqa: F401
    InstallerState, InstallJob, mcp_installs_root, _now_iso,
)
from .mcp_install_scan import (  # noqa: F401
    clone_repo, static_scan, _parse_vetting_response, vet_with_llm,
    detect_run_command, run_handshake,
)
from .mcp_install_orchestrator import McpInstaller, _new_job  # noqa: F401
