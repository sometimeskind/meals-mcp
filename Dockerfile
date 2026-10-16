FROM python:3.14-slim AS prod

COPY meals_mcp/ /build/meals_mcp/
COPY pyproject.toml requirements.txt /build/
RUN pip install --no-cache-dir -r /build/requirements.txt /build \
 && rm -rf /build

# Non-root; the pod also sets runAsNonRoot and a read-only root filesystem.
USER 65532:65532
EXPOSE 8000 9090
CMD ["python", "-m", "meals_mcp"]

FROM prod AS dev
USER root
COPY requirements-dev.txt /tmp/reqs-dev.txt
RUN pip install --no-cache-dir -r /tmp/reqs-dev.txt
COPY pyproject.toml /app/
COPY tests/ /app/tests/
WORKDIR /app
USER 65532:65532
ENTRYPOINT []
CMD ["pytest", "tests/"]
