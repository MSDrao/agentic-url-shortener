# Use the project's virtualenv automatically when it exists, so `make` works even if it isn't activated.
PY ?= $(if $(wildcard .venv/bin/python),$(abspath .venv/bin/python),python3)

.PHONY: install test test-service test-orchestrator demo serve sandbox-image clean

install:
	$(PY) -m pip install -r requirements.txt

test: test-service test-orchestrator

test-service:
	cd service && $(PY) -m pytest --cov=shortener --cov-report=term-missing

test-orchestrator:
	$(PY) -m pytest

demo:
	$(PY) -m orchestrator demo

sandbox-image:
	docker build -f Dockerfile.sandbox -t orchestrator-sandbox:py311 .

serve:
	cd service && $(PY) -m uvicorn shortener.main:app --reload --port 8000

clean:
	rm -rf runs .pytest_cache service/.pytest_cache service/shortener.db*
