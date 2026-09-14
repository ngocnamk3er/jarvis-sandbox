VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn
PYTEST := $(VENV)/bin/pytest

.PHONY: install install-dev run dev test shell

install:
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt

install-dev:
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements-dev.txt

# Local dev: bash in WORKSPACE_DIR, no cluster, no isolation — same server
# that runs inside a real agent-sandbox pod (app.agent.agentsandbox_server).
run:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8003

dev:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8003 --reload --reload-dir app

test:
	$(PYTEST) -q

shell:
	$(PYTHON)
