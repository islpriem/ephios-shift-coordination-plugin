.PHONY: setup format test check e2e build dev-up dev-down

setup:
	./scripts/run python3 scripts/project.py setup

format:
	./scripts/run uv run --locked ruff format .
	./scripts/run uv run --locked ruff check --fix .

test:
	./scripts/run uv run --locked coverage run -m pytest
	./scripts/run uv run --locked coverage report

check:
	./scripts/run python3 scripts/project.py check

e2e:
	./scripts/run python3 scripts/project.py e2e

build:
	./scripts/run python3 scripts/project.py build

dev-up:
	./scripts/run python3 scripts/project.py up

dev-down:
	./scripts/run python3 scripts/project.py down
