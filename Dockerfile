# Runtime image for the scheduler service. Development still happens on the
# host; this exists so the scheduled pipeline can run as a container.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies are installed from the manifest alone, before the source is
# copied, so that editing code does not invalidate the dependency layer.
COPY pyproject.toml README.md ./
RUN mkdir -p src/hansard && touch src/hansard/__init__.py \
    && pip install --no-cache-dir .

COPY src/ ./src/
COPY migrations/ ./migrations/
RUN pip install --no-cache-dir --no-deps .

# Never run as root: a container that only reads a public API and writes to
# Postgres has no need for it.
RUN useradd --create-home --uid 10001 hansard
USER hansard

ENTRYPOINT ["hansard"]
CMD ["--help"]
