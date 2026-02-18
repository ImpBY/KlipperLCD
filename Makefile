PYTHON ?= python3
REPO_NAME := $(notdir $(CURDIR))

# Dev/Test venv (inside repository)
DEV_VENV ?= .venv
DEV_VENV_BIN := $(DEV_VENV)/bin
DEV_PIP := $(DEV_VENV_BIN)/pip

# Service runtime context (derived from current user running make)
SERVICE_USER ?= $(shell id -un)
SERVICE_HOME ?= $(shell getent passwd "$(SERVICE_USER)" | cut -d: -f6)
SERVICE_REPO_DIR ?= $(SERVICE_HOME)/$(REPO_NAME)
SERVICE_VENV ?= $(SERVICE_HOME)/$(REPO_NAME)-venv
SERVICE_VENV_BIN := $(SERVICE_VENV)/bin
SERVICE_PIP := $(SERVICE_VENV_BIN)/pip

SERVICE_NAME ?= $(REPO_NAME).service
SERVICE_TEMPLATE ?= service.template
SERVICE_TARGET ?= /etc/systemd/system/$(SERVICE_NAME)

SERVICE_ENV_DIR ?= $(SERVICE_HOME)/.config/$(REPO_NAME)
SERVICE_ENV_FILE ?= $(SERVICE_ENV_DIR)/service.env
SERVICE_ENV_TEMPLATE ?= service.env.example

PRINTER_HOST ?= btt
PRINTER_USER ?= mcu
PRINTER_PATH ?= /home/$(PRINTER_USER)/$(REPO_NAME)
RSYNC ?= rsync
SYNC_EXCLUDES := \
	--exclude '.git/' \
	--exclude '.venv/' \
	--exclude '__pycache__/' \
	--exclude '*.pyc' \
	--exclude '.pytest_cache/' \
	--exclude 'build/' \
	--exclude 'dist/'

.PHONY: test clean deps create update remove install uninstall upgrade config sync

# ----- Development / testing (repo-local only) -----

test:
	@echo "[TODO] Тесты не настроены. Добавьте тесты (например, pytest) и замените эту заглушку."

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
	find . -type d -name "build" -prune -exec rm -rf {} +
	find . -type d -name "dist" -prune -exec rm -rf {} +

deps: create
	$(DEV_PIP) install --upgrade pip setuptools wheel
	$(DEV_PIP) install -r requirements.txt

create:
	$(PYTHON) -m venv $(DEV_VENV)

update: deps
	$(DEV_PIP) install --upgrade -r requirements.txt

remove:
	rm -rf $(DEV_VENV)

# ----- Service install / update only -----

config:
	mkdir -p "$(SERVICE_ENV_DIR)"
	@if [ ! -f "$(SERVICE_ENV_FILE)" ]; then \
		cp "$(SERVICE_ENV_TEMPLATE)" "$(SERVICE_ENV_FILE)"; \
		echo "Created $(SERVICE_ENV_FILE)"; \
	else \
		while IFS= read -r line; do \
			case "$$line" in ''|'#'*) continue ;; esac; \
			key="$${line%%=*}"; \
			if ! grep -qE "^[[:space:]]*$$key=" "$(SERVICE_ENV_FILE)"; then \
				echo "$$line" >> "$(SERVICE_ENV_FILE)"; \
			fi; \
		done < "$(SERVICE_ENV_TEMPLATE)"; \
		echo "Updated $(SERVICE_ENV_FILE) with newly introduced keys only"; \
	fi

install: config
	$(PYTHON) -m venv $(SERVICE_VENV)
	$(SERVICE_PIP) install --upgrade pip setuptools wheel
	$(SERVICE_PIP) install --upgrade -r requirements.txt
	$(SERVICE_PIP) install --upgrade .
	sed \
		-e "s/__SERVICE_USER__/$(SERVICE_USER)/g" \
		-e "s/__REPO_NAME__/$(REPO_NAME)/g" \
		$(SERVICE_TEMPLATE) | sudo tee $(SERVICE_TARGET) >/dev/null
	sudo chmod 644 $(SERVICE_TARGET)
	sudo systemctl daemon-reload
	sudo systemctl enable --now $(SERVICE_NAME)

uninstall:
	sudo systemctl disable --now $(SERVICE_NAME) || true
	sudo rm -f $(SERVICE_TARGET)
	sudo systemctl daemon-reload
	rm -rf $(SERVICE_VENV)

upgrade: config
	$(PYTHON) -m venv $(SERVICE_VENV)
	$(SERVICE_PIP) install --upgrade pip setuptools wheel
	$(SERVICE_PIP) install --upgrade -r requirements.txt
	$(SERVICE_PIP) install --upgrade .
	sed \
		-e "s/__SERVICE_USER__/$(SERVICE_USER)/g" \
		-e "s/__REPO_NAME__/$(REPO_NAME)/g" \
		$(SERVICE_TEMPLATE) | sudo tee $(SERVICE_TARGET) >/dev/null
	sudo chmod 644 $(SERVICE_TARGET)
	sudo systemctl daemon-reload
	sudo systemctl restart $(SERVICE_NAME)

sync:
	ssh $(PRINTER_USER)@$(PRINTER_HOST) "mkdir -p '$(PRINTER_PATH)'"
	$(RSYNC) -az --delete $(SYNC_EXCLUDES) ./ $(PRINTER_USER)@$(PRINTER_HOST):$(PRINTER_PATH)/
