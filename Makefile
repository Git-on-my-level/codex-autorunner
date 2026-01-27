VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip

# Prefer venv python if it exists
PYTHON := $(shell if [ -x $(VENV_PYTHON) ]; then echo $(VENV_PYTHON); else echo python3; fi)

export PATH := $(CURDIR)/$(VENV)/bin:$(PATH)
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

.PHONY: install dev hooks build test check format serve serve-dev launchd-hub deadcode-baseline venv venv-dev setup npm-install car-artifacts lint-html dom-check frontend-check

build: npm-install
	pnpm build

install:
	$(PYTHON) -m pip install .

dev:
	$(PYTHON) -m pip install -e .[dev]

venv: $(VENV_PYTHON)

$(VENV_PYTHON):
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip

venv-dev: $(VENV)/.installed-dev

$(VENV)/.installed-dev: $(VENV_PYTHON) pyproject.toml
	$(VENV_PIP) install -e .[dev]
	@touch $(VENV)/.installed-dev

setup: venv-dev npm-install hooks
	@echo "Setup complete. Venv is automatically used by Make targets."

npm-install: node_modules/.installed

node_modules/.installed: package.json pnpm-lock.yaml
	@if command -v pnpm >/dev/null 2>&1; then \
		echo "Using pnpm..."; \
		pnpm install; \
	elif command -v corepack >/dev/null 2>&1; then \
		echo "pnpm not found; using corepack to install pnpm..."; \
		corepack enable; \
		corepack prepare pnpm@9.15.4 --activate; \
		pnpm install; \
	else \
		echo "Missing pnpm. Install it or enable corepack (Node >=16)." >&2; \
		exit 1; \
	fi
	@touch node_modules/.installed

hooks:
	git config core.hooksPath .githooks

test:
	$(PYTHON) -m pytest -m "not integration"

test-integration:
	$(PYTHON) -m pytest -m integration

check:
	./scripts/check.sh
	@if [ -d node_modules ]; then \
		pnpm lint:html && pnpm test:dom; \
	else \
		echo "Skipping frontend checks (node_modules missing). Run 'make npm-install' first." >&2; \
	fi

lint-html: npm-install
	pnpm lint:html

dom-check: npm-install
	pnpm test:dom

frontend-check: lint-html dom-check

format:
	$(PYTHON) -m black src tests
	$(PYTHON) -m ruff check --fix src tests
	@if [ -d node_modules ]; then \
		echo "Fixing JS files (eslint)..."; \
		./node_modules/.bin/eslint --fix "src/codex_autorunner/static_src/**/*.ts" || true; \
	fi

deadcode-baseline:
	$(PYTHON) scripts/deadcode.py --update-baseline

serve: build
	$(PYTHON) -m codex_autorunner.cli serve --host $(HOST) --port $(PORT)

serve-dev: venv-dev
	$(VENV_PYTHON) -m uvicorn codex_autorunner.server:create_hub_app --factory --reload --host $(HOST) --port $(PORT) --reload-dir src --reload-include '*.py' --reload-include '*.js' --reload-include '*.css' --reload-include '*.html' --reload-include '*.json' --reload-exclude '**/worktrees/**' --reload-exclude '**/.codex-autorunner/**' --reload-exclude '.codex-autorunner/**' --timeout-graceful-shutdown 1

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
		scripts/safe-refresh-local-mac-hub.sh

car-artifacts:
	scripts/car-artifact-sizes.sh
