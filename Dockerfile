FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN pip install --no-cache-dir uv==0.10.4

WORKDIR /app

# Dependencies first so the layer is cached; uv.lock must be committed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Mount a directory with the batch and the universe file, then pass the run arguments:
#   docker run --rm -v "$PWD:/data" signalpost --input /data/batch.jsonl \
#     --universe /data/signalpost-company-universe-2025.jsonl.gz --out /data/out/run-001 \
#     --run-id run-001 --expected-count 100
# Add -e BRAVE_API_KEY=... to enable Brave candidate discovery.
ENTRYPOINT ["uv", "run", "--frozen", "python", "-m", "signalpost", "run"]
CMD ["--help"]
