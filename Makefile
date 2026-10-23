PYTHON ?= python$(shell cat .python-version)
VENV := .venv
BIN := $(VENV)/bin

.PHONY: dev test lint hooks clean

$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -e '.[dev]'

dev: $(BIN)/activate hooks
	. $(BIN)/activate && uvicorn stockmanager.app:app --reload

hooks:
	cp scripts/pre-commit-hook.sh .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit

test: $(BIN)/activate
	$(BIN)/pytest

lint: $(BIN)/activate
	$(BIN)/ruff check src tests
	$(BIN)/mypy src tests

clean:
	rm -rf $(VENV) .ruff_cache .mypy_cache .pytest_cache build dist *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -not -path './.git/*' -prune -exec rm -rf {} +
