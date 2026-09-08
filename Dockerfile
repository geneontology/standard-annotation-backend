# python@3.13.15
FROM python@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.12.9

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

RUN groupadd --gid 10001 sab \
    && useradd --uid 10001 --gid sab --create-home --shell /usr/sbin/nologin sab \
    && chown -R sab:sab /app

USER sab

EXPOSE 8000

CMD ["uvicorn", "standard_annotation_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
