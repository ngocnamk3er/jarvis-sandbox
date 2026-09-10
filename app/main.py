"""Entrypoint. One image, two roles — `SANDBOX_ROLE` picks which FastAPI app
gets built. `uvicorn app.main:app` works for both (k8s Deployment for the
orchestrator, the pod spec the orchestrator writes for each agent).
"""

import logging

from app.core.config import settings

# uvicorn only configures its own loggers; make our `logging.getLogger(__name__)`
# calls (pool claim/refill/GC, k8s config source) actually show up in `kubectl
# logs`.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

if settings.SANDBOX_ROLE == "orchestrator":
    from app.orchestrator.app import build_app
else:
    from app.agent.app import build_app

app = build_app()
