PYTHON ?= python
HOST ?= 127.0.0.1
PORT ?= 4173

.PHONY: install dev hooks test check serve serve-dev

install:
	$(PYTHON) -m pip install .

dev:
	$(PYTHON) -m pip install -e .[dev]

hooks:
	git config core.hooksPath .githooks

test:
	$(PYTHON) -m pytest

check:
	./scripts/check.sh

serve:
	$(PYTHON) -m codex_autorunner.cli serve --host $(HOST) --port $(PORT)

serve-dev:
	uvicorn codex_autorunner.server:create_app --factory --reload --host $(HOST) --port $(PORT) --reload-dir src --reload-dir .codex-autorunner
