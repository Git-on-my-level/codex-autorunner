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

.PHONY: install dev hooks test check format serve serve-dev launchd-hub

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

format:
	$(PYTHON) -m black src tests

serve:
	$(PYTHON) -m codex_autorunner.cli serve --host $(HOST) --port $(PORT)

serve-dev:
	uvicorn codex_autorunner.server:create_app --factory --reload --host $(HOST) --port $(PORT) --reload-dir src --reload-dir .codex-autorunner

launchd-hub:
	@mkdir -p $(dir $(LAUNCH_AGENT))
	@cat > $(LAUNCH_AGENT) <<-'EOF'
	<?xml version="1.0" encoding="UTF-8"?>
	<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
	<plist version="1.0">
	<dict>
	  <key>Label</key>
	  <string>com.codex.autorunner</string>
	  <key>ProgramArguments</key>
	  <array>
	    <string>/bin/sh</string>
	    <string>-lc</string>
	    <string>PATH=$(NVM_BIN):$(LOCAL_BIN):$(PY39_BIN):$$PATH; codex-autorunner hub serve --host $(HUB_HOST) --port $(HUB_PORT) --base-path $(HUB_BASE_PATH) --path $(CAR_ROOT)</string>
	  </array>
	  <key>WorkingDirectory</key>
	  <string>$(CAR_ROOT)</string>
	  <key>RunAtLoad</key>
	  <true/>
	  <key>KeepAlive</key>
	  <true/>
	  <key>StandardOutPath</key>
	  <string>$(CAR_ROOT)/.codex-autorunner/codex-autorunner-hub.log</string>
	  <key>StandardErrorPath</key>
	  <string>$(CAR_ROOT)/.codex-autorunner/codex-autorunner-hub.log</string>
	</dict>
	</plist>
	EOF
	@launchctl unload -w $(LAUNCH_AGENT) >/dev/null 2>&1 || true
	launchctl load -w $(LAUNCH_AGENT)
	launchctl kickstart -k gui/$$(id -u)/com.codex.autorunner

.PHONY: refresh-launchd
refresh-launchd:
	@echo "Reinstalling codex-autorunner into pipx venv at $(PIPX_VENV)..."
	$(PIPX_PYTHON) -m pip install --force-reinstall $(CURDIR)
	@echo "Reloading launchd agent $(LAUNCH_AGENT)..."
	launchctl unload $(LAUNCH_AGENT) >/dev/null 2>&1 || true
	launchctl load -w $(LAUNCH_AGENT)
	launchctl kickstart -k gui/$$(id -u)/$(LAUNCH_LABEL)
