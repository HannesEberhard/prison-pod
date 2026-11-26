FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --compile-bytecode
COPY main.py manifests.py ./
# kopf calls getpass.getuser(), which needs a passwd entry.
RUN useradd --uid 1000 --no-create-home kopf
USER 1000
ENV PATH=/app/.venv/bin:$PATH
CMD ["kopf", "run", "--standalone", "--all-namespaces", "--quiet", "main.py"]
