VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn

.PHONY: install run dev shell

install:
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt

run:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8003

dev:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8003 --reload --reload-dir app

shell:
	$(PYTHON)
