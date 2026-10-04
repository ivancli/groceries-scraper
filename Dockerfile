# One image for every Site: the CLI, Chromium for `render: browser`, and the baked-in configs.
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra postgres --extra s3 --no-install-project
COPY src ./src
# Editable on purpose: the loader finds defaults.yaml beside src/, i.e. in /app.
RUN uv sync --locked --no-dev --extra postgres --extra s3

FROM python:3.12-slim
ENV PATH=/app/.venv/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY --from=builder /app/.venv /app/.venv
# The venv's own Playwright installs the Chromium build it was locked against.
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 scraper
WORKDIR /app
COPY --from=builder /app/src ./src
COPY defaults.yaml ./
COPY sites ./sites
RUN mkdir runs && chown scraper runs
USER scraper
ENTRYPOINT ["scrape"]
CMD ["--help"]
