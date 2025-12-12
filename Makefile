PYTHON ?= python
HOST ?= 127.0.0.1
PORT ?= 4173
HUB_HOST ?= 127.0.0.1
HUB_PORT ?= 4517
HUB_BASE_PATH ?= /car
CAR_ROOT ?= $(HOME)/car-workspace
LAUNCH_AGENT ?= $(HOME)/Library/LaunchAgents/com.codex.autorunner.plist
LAUNCH_LABEL ?= com.codex.autorunner
NVM_BIN ?= $(HOME)/.nvm/versions/node/v22.12.0/bin
LOCAL_BIN ?= $(HOME)/.local/bin
PY39_BIN ?= $(HOME)/Library/Python/3.9/bin
PIPX_ROOT ?= $(HOME)/.local/pipx
PIPX_VENV ?= $(PIPX_ROOT)/venvs/codex-autorunner
PIPX_PYTHON ?= $(PIPX_VENV)/bin/python

.PHONY: install dev hooks test check serve serve-dev launchd-hub

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

launchd-hub:
	@LABEL="$(LAUNCH_LABEL)" \
		LAUNCH_AGENT="$(LAUNCH_AGENT)" \
		CAR_ROOT="$(CAR_ROOT)" \
		HUB_HOST="$(HUB_HOST)" \
		HUB_PORT="$(HUB_PORT)" \
		HUB_BASE_PATH="$(HUB_BASE_PATH)" \
		NVM_BIN="$(NVM_BIN)" \
		LOCAL_BIN="$(LOCAL_BIN)" \
		PY39_BIN="$(PY39_BIN)" \
		scripts/launchd-hub.sh

.PHONY: refresh-launchd
refresh-launchd:
	@LABEL="$(LAUNCH_LABEL)" \
		PLIST_PATH="$(LAUNCH_AGENT)" \
		PACKAGE_SRC="$(CURDIR)" \
		PIPX_VENV="$(PIPX_VENV)" \
		PIPX_PYTHON="$(PIPX_PYTHON)" \
		scripts/refresh-local-mac-hub.sh
