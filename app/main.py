"""Entrypoint. jarvis-sandbox's own orchestrator/agent split is gone
(decommissioned 2026-09-14 — real traffic moved to kubernetes-sigs/agent-sandbox,
see AGENTSANDBOX-MIGRATION.md) — this image now only ever runs the
agent-sandbox adapter, `app.agent.agentsandbox_server`. `uvicorn app.main:app`
and `Dockerfile.agentsandbox`'s own CMD both resolve to the same app object.
"""

import logging

from app.agent.agentsandbox_server import app  # noqa: F401 — re-exported for `uvicorn app.main:app`

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
