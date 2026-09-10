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

# Local dev = the "agent" role: bash in WORKSPACE_DIR, no cluster, no isolation.
run:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8003

dev:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8003 --reload --reload-dir app

test:
	$(PYTEST) -q

shell:
	$(PYTHON)
