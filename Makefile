# TradeBot developer + deploy targets.
#
# The backend runs in Docker with ./backend bind-mounted to /app (see
# docker-compose.yml), so a container restart ships whatever is on disk. `make
# restart` runs the test suite FIRST, so a red suite can never be deployed to the box
# (review finding #12). CI runs the same suite on every backend change — see
# .github/workflows/backend-tests.yml.

BACKEND   ?= backend
CONTAINER ?= nomadcurie_backend
# Fallback interpreter if the repo-root virtualenv is absent. The recipe prefers
# ./.venv/bin/python when it exists (detected in-shell so paths with spaces work).
PYTHON    ?= python3

.PHONY: test test-backend restart deploy

## test — run the backend unit/contract suite. Self-contained: it mocks or gracefully
## degrades Postgres, Redis and every broker, so it needs no database or network.
test test-backend:
	@PY="$(CURDIR)/.venv/bin/python"; \
	[ -x "$$PY" ] || PY="$(PYTHON)"; \
	echo "Using interpreter: $$PY"; \
	cd "$(BACKEND)" && "$$PY" -m pytest tests

## restart / deploy — gate the bind-mount deploy on a green suite, then restart the
## backend container. Aborts (leaving the running container untouched) if tests fail.
restart deploy: test
	docker restart $(CONTAINER)
