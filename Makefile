VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip

# Prefer venv python if it exists
PYTHON := $(shell if [ -x $(VENV_PYTHON) ]; then echo $(VENV_PYTHON); else echo python3; fi)
PYTHON_ABS := $(abspath $(PYTHON))
PYTEST_FAST_WORKERS ?= 4

export PATH := $(CURDIR)/$(VENV)/bin:$(PATH)
HOST ?= 127.0.0.1
PORT ?= 4173
# `make serve-onboarding`: temp hub; default port avoids clashing with `make serve`.
ONBOARDING_HOST ?= $(HOST)
ONBOARDING_PORT ?= 4174
WEB_UI_SCREEN_MODE ?= fixture
WEB_UI_SCREEN_OUT ?= .codex-autorunner/render/web_ui_samples/latest
WEB_UI_SCREEN_VIEWPORT ?=
WEB_UI_SCREEN_VIEWPORTS ?= $(if $(WEB_UI_SCREEN_VIEWPORT),$(WEB_UI_SCREEN_VIEWPORT),1440x1000 390x844)
WEB_UI_SCREEN_HOST ?= $(HOST)
WEB_UI_SCREEN_PORT ?= 0
WEB_UI_SCREEN_HUB_ROOT ?= $(CAR_ROOT)
WEB_UI_SCREEN_ARGS ?=
HUB_HOST ?= 127.0.0.1
HUB_PORT ?= 4517
HUB_BASE_PATH ?= /car
# Hub filesystem root for serve-hub; `make serve` also falls back to this repo if no config under CAR_ROOT.
CAR_ROOT ?= $(HOME)/car-workspace
# Vite dev server port (`make serve`). Hub API uses PORT (default 4173).
WEB_DEV_PORT ?= 5173
LAUNCH_AGENT ?= $(HOME)/Library/LaunchAgents/com.codex.autorunner.plist
LAUNCH_LABEL ?= com.codex.autorunner
NVM_BIN ?= $(HOME)/.nvm/versions/node/v22.12.0/bin
LOCAL_BIN ?= $(HOME)/.local/bin
PY39_BIN ?= $(HOME)/Library/Python/$(shell python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')/bin
PIPX_ROOT ?= $(HOME)/.local/pipx
PIPX_VENV ?= $(PIPX_ROOT)/venvs/codex-autorunner
PIPX_PYTHON ?= $(PIPX_VENV)/bin/python

.PHONY: install dev hooks build web-build test test-fast test-full test-chat-platform-contract test-chat-surface-lab test-managed-thread-cutover check check-full check-web-core-contract check-extended preflight-hub-startup format serve serve-hub serve-onboarding web-ui-screens launchd-hub deadcode-baseline venv venv-dev setup npm-install car-artifacts agent-compatibility-check agent-compatibility-refresh protocol-schemas-check protocol-schemas-refresh typecheck-strict perf-idle-cpu perf-chat-latency-budgets perf-chat-seeded-exploration

build: web-build

web-build: npm-install
	pnpm web:build

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
	git config --local core.hooksPath .githooks

test: test-fast

test-fast:
	$(PYTHON) -m pytest -m "not integration and not slow" -n $(PYTEST_FAST_WORKERS)

test-full:
	$(PYTHON) -m pytest -m "not integration"

# Cross-platform chat contract/shape guardrails.
# Add additional platform suites here as new chat adapters land.
test-chat-platform-contract:
	$(PYTHON) -m pytest -q \
		tests/adapters/chat/test_command_contract.py \
		tests/adapters/chat/test_command_ingress_parity.py \
		tests/adapters/discord/test_service_routing.py \
		tests/adapters/discord/test_interactions_parse.py \
		tests/adapters/chat/test_parity_checker.py \
		tests/test_telegram_command_contract.py \
		tests/test_doctor_checks.py::test_chat_doctor_checks_use_parity_contract_group \
		tests/test_doctor_checks.py::test_chat_doctor_checks_failures_are_actionable

test-chat-surface-lab:
	$(PYTHON) -m pytest -q \
		tests/chat_surface_lab/test_scenario_corpus.py \
		tests/chat_surface_lab/test_artifact_manifest.py \
		tests/chat_surface_lab/test_latency_budgets.py \
		tests/chat_surface_lab/test_seeded_exploration.py \
		tests/chat_surface_lab/test_incident_replay.py
	$(PYTHON) scripts/chat_surface_latency_budgets.py --profile check-chat-surface-lab

test-managed-thread-cutover:
	$(PYTHON) -m pytest -q \
		tests/test_backend_run_event_contract.py \
		tests/test_hub_supervisor.py \
		tests/test_pma_managed_threads_lifecycle.py \
		tests/test_pma_managed_threads_turns.py \
		tests/adapters/chat/test_orchestration_guardrails.py \
		tests/test_telegram_pma_routing.py \
		tests/test_telegram_bot_integration.py \
		tests/test_telegram_turn_queue.py \
		tests/test_telegram_status_rate_limits.py \
		tests/adapters/discord/test_service_routing.py \
		tests/adapters/discord/test_message_turns.py \
		tests/adapters/discord/test_message_turns_transient_progress.py \
		tests/test_redaction.py

test-integration:
	$(PYTHON) -m pytest -m integration

typecheck-strict:
	$(PYTHON) -m mypy src/codex_autorunner

check:
	./scripts/check.sh

check-full:
	./scripts/check.sh --full

check-web-core-contract:
	./scripts/check.sh --lane web-core-contract

check-extended: check-full
	$(MAKE) test-chat-platform-contract PYTHON="$(PYTHON)"

preflight-hub-startup:
	$(PYTHON) -m pytest -q tests/test_hub_app_context.py::test_hub_lifespan_reaper_uses_config_root

protocol-schemas-check:
	$(MAKE) agent-compatibility-check PYTHON="$(PYTHON)"

agent-compatibility-check:
	$(PYTHON) -m pytest tests/test_protocol_schemas.py -q
	$(PYTHON) scripts/check_protocol_drift.py

protocol-schemas-refresh:
	$(MAKE) agent-compatibility-refresh PYTHON="$(PYTHON)"

agent-compatibility-refresh:
	$(PYTHON) scripts/update_agent_compatibility_lock.py
	$(PYTHON) -m codex_autorunner.cli protocol refresh

format:
	$(PYTHON) -m black src tests
	$(PYTHON) -m ruff check --fix src tests

deadcode-baseline:
	$(PYTHON) scripts/deadcode.py --update-baseline

# Development: hub API (Python --reload) + Web Vite dev server (HMR). Use the printed Vite URL.
serve: venv-dev npm-install
	@HOST=$(HOST) PORT=$(PORT) WEB_DEV_PORT=$(WEB_DEV_PORT) CAR_ROOT="$(CAR_ROOT)" CAR_HUB_ROOT="$(if $(CAR_HUB_ROOT),$(CAR_HUB_ROOT),$(CAR_ROOT))" "$(CURDIR)/scripts/serve-dev-hub.sh"

# Production-style hub: pre-built Web static assets, no Python/Vite reload.
serve-hub: web-build
	@PORT_PID=$$(lsof -t -nP -iTCP:$(PORT) -sTCP:LISTEN 2>/dev/null | head -n 1); \
	if [ -n "$$PORT_PID" ]; then \
		echo "Port $(PORT) is already in use by PID $$PORT_PID. Stop the existing server before running \`make serve-hub\`." >&2; \
		lsof -nP -iTCP:$(PORT) -sTCP:LISTEN >&2 || true; \
		exit 1; \
	fi
	$(PYTHON) -m codex_autorunner.cli hub serve --path "$(CAR_ROOT)" --host $(HOST) --port $(PORT)

# Hub initialized with `car init --mode hub` in a new temp directory (no repos, clean manifest).
# Prints URLs for real-server onboarding and optional client-mocked PMA.
serve-onboarding: web-build
	@set -e; \
	PORT_PID=$$(lsof -t -nP -iTCP:$(ONBOARDING_PORT) -sTCP:LISTEN 2>/dev/null | head -n 1); \
	if [ -n "$$PORT_PID" ]; then \
		echo "Port $(ONBOARDING_PORT) is already in use by PID $$PORT_PID. Stop the existing server before running \`make serve-onboarding\`." >&2; \
		lsof -nP -iTCP:$(ONBOARDING_PORT) -sTCP:LISTEN >&2 || true; \
		exit 1; \
	fi; \
	ROOT=$$(mktemp -d); \
	echo "==> Fresh hub root: $$ROOT"; \
	"$(PYTHON_ABS)" -m codex_autorunner.cli init --mode hub "$$ROOT"; \
	BASE="http://$(ONBOARDING_HOST):$(ONBOARDING_PORT)"; \
	echo ""; \
	echo "==> A) Real APIs (use after stopping any other server on this port): Open"; \
	echo "      $$BASE/?carOnboarding=1&uiMockStrip=1"; \
	echo "   (re-shows the setup walkthrough strip; add &view=pma if you want PMA open when PMA is enabled in config.)"; \
	echo ""; \
	echo "==> B) Client mocks: empty hub + PMA without server PMA config: Open"; \
	echo "      $$BASE/?uiMock=onboarding&view=pma&carOnboarding=1&uiMockStrip=1"; \
	echo ""; \
	exec "$(PYTHON_ABS)" -m codex_autorunner.cli hub serve --path "$$ROOT" --host $(ONBOARDING_HOST) --port $(ONBOARDING_PORT)

web-ui-screens: web-build
	@set --; \
	for viewport in $(WEB_UI_SCREEN_VIEWPORTS); do \
		set -- "$$@" --viewport "$$viewport"; \
	done; \
	$(PYTHON) scripts/web_ui_screens.py \
		--mode '$(WEB_UI_SCREEN_MODE)' \
		--host '$(WEB_UI_SCREEN_HOST)' \
		--port '$(WEB_UI_SCREEN_PORT)' \
		--hub-root '$(WEB_UI_SCREEN_HUB_ROOT)' \
		--out-dir '$(WEB_UI_SCREEN_OUT)' \
		"$$@" \
		$(WEB_UI_SCREEN_ARGS)

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

PROFILE ?= hub_only

perf-idle-cpu:
	$(PYTHON) scripts/idle_cpu_soak.py --profile $(PROFILE)

perf-chat-latency-budgets:
	$(PYTHON) scripts/chat_surface_latency_budgets.py

perf-chat-seeded-exploration:
	$(PYTHON) scripts/chat_surface_seeded_exploration.py
