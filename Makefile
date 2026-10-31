PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin
PY     := $(BIN)/python
STATE  ?= .demo-state
CLI    := $(PY) -m deployctl
M      := examples/manifests
OPTS   := --state-dir $(STATE) --artifact-dir examples/artifacts
HEALTHY := --health-profile examples/health/default.json
OUTAGE  := --health-profile examples/health/dependency-outage.json

.PHONY: help install lock fmt lint test build zipapp smoke demo ci dist-tarball clean

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

$(VENV)/.installed: pyproject.toml requirements-dev.txt
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pip install -e . --no-deps
	touch $@

install: $(VENV)/.installed ## create venv with pinned dev deps

lock: ## regenerate pinned requirements-dev.txt
	rm -rf .venv-lock && $(PYTHON) -m venv .venv-lock
	.venv-lock/bin/pip install -e '.[dev]'
	.venv-lock/bin/pip freeze --exclude-editable > requirements-dev.txt
	rm -rf .venv-lock

fmt: install ## auto-format and fix lint
	$(BIN)/ruff format src tests scripts
	$(BIN)/ruff check --fix src tests scripts

lint: install ## format check, lint, type check
	$(BIN)/ruff format --check src tests scripts
	$(BIN)/ruff check src tests scripts
	$(BIN)/mypy

test: install ## run tests with coverage gate
	$(BIN)/pytest

build: install ## wheel + sdist + single-file zipapp into dist/
	rm -rf dist build
	$(PY) -m build
	$(MAKE) zipapp

zipapp:
	mkdir -p build/zipapp dist
	cp -R src/deployctl build/zipapp/
	find build/zipapp -name '__pycache__' -prune -exec rm -rf {} +
	$(PY) -m zipapp build/zipapp -m 'deployctl.cli:run' -p '/usr/bin/env python3' -c -o dist/deployctl.pyz

smoke: build ## run the built artifact, including exit-code propagation
	$(PYTHON) dist/deployctl.pyz --version
	$(PYTHON) dist/deployctl.pyz validate --manifest $(M)/orders-api-1.4.2.json --artifact-dir examples/artifacts
	$(PYTHON) dist/deployctl.pyz validate --manifest $(M)/invalid-schema.json --artifact-dir examples/artifacts; test $$? -eq 3

demo: install ## end-to-end scenario used for README example output
	rm -rf $(STATE)
	@echo "== 1. first deploy 1.4.2 (expect SUCCEEDED)"
	$(CLI) deploy $(OPTS) $(HEALTHY) --manifest $(M)/orders-api-1.4.2.json; echo "exit=$$?"
	@echo "== 2. same manifest again (expect NO_OP)"
	$(CLI) deploy $(OPTS) $(HEALTHY) --manifest $(M)/orders-api-1.4.2.json; echo "exit=$$?"
	@echo "== 3. unhealthy 1.5.0 (expect ROLLED_BACK to 1.4.2)"
	$(CLI) deploy $(OPTS) $(HEALTHY) --manifest $(M)/orders-api-1.5.0.json; echo "exit=$$?"
	@echo "== 4. retry 1.5.0 (expect BLOCKED known-bad)"
	$(CLI) deploy $(OPTS) $(HEALTHY) --manifest $(M)/orders-api-1.5.0.json; echo "exit=$$?"
	@echo "== 5. flaky-then-healthy 1.5.1 (expect SUCCEEDED)"
	$(CLI) deploy $(OPTS) $(HEALTHY) --manifest $(M)/orders-api-1.5.1.json; echo "exit=$$?"
	@echo "== 6. invalid manifest (expect REJECTED)"
	$(CLI) deploy $(OPTS) $(HEALTHY) --manifest $(M)/invalid-schema.json; echo "exit=$$?"
	@echo "== 7. checksum mismatch (expect REJECTED)"
	$(CLI) deploy $(OPTS) $(HEALTHY) --manifest $(M)/checksum-mismatch.json; echo "exit=$$?"
	@echo "== 8. 1.6.0 during dependency outage (expect ROLLBACK_FAILED / NEEDS_ATTENTION)"
	$(CLI) deploy $(OPTS) $(OUTAGE) --manifest $(M)/orders-api-1.6.0.json; echo "exit=$$?"
	@echo "== 9. deploy while NEEDS_ATTENTION (expect BLOCKED)"
	$(CLI) deploy $(OPTS) $(HEALTHY) --manifest $(M)/orders-api-1.5.1.json; echo "exit=$$?"
	@echo "== 10. operator recovers after outage (expect RECOVERED @ 1.5.1)"
	$(CLI) recover --state-dir $(STATE) $(HEALTHY) --service orders-api --env staging --reason "outage over"; echo "exit=$$?"
	$(CLI) status --state-dir $(STATE) --service orders-api --env staging
	$(CLI) history --state-dir $(STATE) --service orders-api --env staging --limit 15

ci: lint test build smoke ## what CI runs

dist-tarball: ## submission tarball of HEAD
	git archive --format=tar.gz --prefix=safe-deploy-controller/ -o ../safe-deploy-controller.tar.gz HEAD

clean:
	rm -rf $(VENV) build dist .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov $(STATE) .debug-state
