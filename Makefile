SHELL := /bin/bash
UNAME_S := $(shell uname -s)

SYSTEM_PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python
PYTHON ?= $(VENV_PYTHON)
SCRIPT := parentmail_watch.py
OP ?= op
PARENTMAIL_HEADLESS ?= $(if $(filter Darwin,$(UNAME_S)),false,true)
PARENTMAIL_DATA_DIR ?= $(CURDIR)/.local/parentmail

# 1Password item setup (override in shell if needed)
PARENTMAIL_OP_ITEM_ID ?= rygji35oy2o7w7czfgvfjuam4q
PARENTMAIL_OP_USERNAME_FIELD ?= username
PARENTMAIL_OP_PASSWORD_FIELD ?= password

.PHONY: help env shell install run run-dry-run run-refresh-attachments

help:
	@echo "Targets:"
	@echo "  make env                     - Create .venv and upgrade pip"
	@echo "  make shell                   - Open an interactive shell with .venv activated"
	@echo "  make install                 - Install Python dependencies and Playwright Chromium"
	@echo "  make run                     - Run ParentMail scan with credentials from 1Password"
	@echo "  make run-dry-run             - Run --dry-run with credentials from 1Password"
	@echo "  make run-refresh-attachments - Run --dry-run --refresh-attachments with credentials from 1Password"
	@echo ""
	@echo "Environment overrides:"
	@echo "  PARENTMAIL_OP_ITEM_ID=<item-id>"
	@echo "  PARENTMAIL_OP_USERNAME_FIELD=<field label, default: username>"
	@echo "  PARENTMAIL_OP_PASSWORD_FIELD=<field label, default: password>"
	@echo "  PYTHON=<python executable>"
	@echo "  SYSTEM_PYTHON=<python used to create .venv, default: python3>"
	@echo "  VENV=<virtual environment directory, default: .venv>"
	@echo "  PARENTMAIL_DATA_DIR=<state dir, default: $(PARENTMAIL_DATA_DIR)>"
	@echo "  PARENTMAIL_HEADLESS=<true|false; default: false on macOS, true elsewhere>"
	@echo "  AGENT_BROWSER_EXECUTABLE_PATH=<chromium path>"

env:
	@set -euo pipefail; \
	if [[ ! -x "$(VENV_PYTHON)" ]]; then "$(SYSTEM_PYTHON)" -m venv "$(VENV)"; fi; \
	"$(VENV_PYTHON)" -m pip install --upgrade pip

shell: env
	@set -euo pipefail; \
	echo "Starting shell with $(VENV) activated"; \
	exec "$$SHELL" -i -c 'source "$(VENV)/bin/activate"; exec "$$SHELL" -i'

install: env
	@set -euo pipefail; \
	"$(PYTHON)" -m pip install playwright pypdf; \
	"$(PYTHON)" -m playwright install chromium

run: env
	@set -euo pipefail; \
	command -v "$(OP)" >/dev/null || { echo "op CLI not found in PATH"; exit 1; }; \
	"$(OP)" whoami >/dev/null; \
	email="$$($(OP) item get "$(PARENTMAIL_OP_ITEM_ID)" --reveal --fields "label=$(PARENTMAIL_OP_USERNAME_FIELD)")"; \
	password="$$($(OP) item get "$(PARENTMAIL_OP_ITEM_ID)" --reveal --fields "label=$(PARENTMAIL_OP_PASSWORD_FIELD)")"; \
	PARENTMAIL_DATA_DIR="$(PARENTMAIL_DATA_DIR)" PARENTMAIL_HEADLESS="$(PARENTMAIL_HEADLESS)" PARENTMAIL_EMAIL="$$email" PARENTMAIL_PASSWORD="$$password" "$(PYTHON)" "$(SCRIPT)"

run-dry-run: env
	@set -euo pipefail; \
	command -v "$(OP)" >/dev/null || { echo "op CLI not found in PATH"; exit 1; }; \
	"$(OP)" whoami >/dev/null; \
	email="$$($(OP) item get "$(PARENTMAIL_OP_ITEM_ID)" --reveal --fields "label=$(PARENTMAIL_OP_USERNAME_FIELD)")"; \
	password="$$($(OP) item get "$(PARENTMAIL_OP_ITEM_ID)" --reveal --fields "label=$(PARENTMAIL_OP_PASSWORD_FIELD)")"; \
	PARENTMAIL_DATA_DIR="$(PARENTMAIL_DATA_DIR)" PARENTMAIL_HEADLESS="$(PARENTMAIL_HEADLESS)" PARENTMAIL_EMAIL="$$email" PARENTMAIL_PASSWORD="$$password" "$(PYTHON)" "$(SCRIPT)" --dry-run

run-refresh-attachments: env
	@set -euo pipefail; \
	command -v "$(OP)" >/dev/null || { echo "op CLI not found in PATH"; exit 1; }; \
	"$(OP)" whoami >/dev/null; \
	email="$$($(OP) item get "$(PARENTMAIL_OP_ITEM_ID)" --reveal --fields "label=$(PARENTMAIL_OP_USERNAME_FIELD)")"; \
	password="$$($(OP) item get "$(PARENTMAIL_OP_ITEM_ID)" --reveal --fields "label=$(PARENTMAIL_OP_PASSWORD_FIELD)")"; \
	PARENTMAIL_DATA_DIR="$(PARENTMAIL_DATA_DIR)" PARENTMAIL_HEADLESS="$(PARENTMAIL_HEADLESS)" PARENTMAIL_EMAIL="$$email" PARENTMAIL_PASSWORD="$$password" "$(PYTHON)" "$(SCRIPT)" --dry-run --refresh-attachments
