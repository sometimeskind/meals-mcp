# Unit tests against a throwaway Postgres 17 in podman (pilot has no Docker daemon).
test:
    #!/usr/bin/env bash
    set -euo pipefail
    podman run -d --rm --name meals-mcp-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=meals \
      -p 127.0.0.1:55433:5432 docker.io/library/postgres:17 >/dev/null
    trap 'podman rm -f meals-mcp-test-pg >/dev/null' EXIT
    until podman exec meals-mcp-test-pg pg_isready -q; do sleep 1; done
    TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55433/meals .venv/bin/pytest tests/
