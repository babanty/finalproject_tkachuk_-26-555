#!/usr/bin/env bash
set -euo pipefail

uv sync --locked
uv run ruff check --fix .
uv run ruff format .
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
