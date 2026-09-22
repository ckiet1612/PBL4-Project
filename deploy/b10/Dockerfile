FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.9.27

WORKDIR /opt/nexa
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable
ENV PATH="/opt/nexa/.venv/bin:${PATH}"

CMD ["nexa-worker"]
