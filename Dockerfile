# Glama inspection image: must start and answer initialize + tools/list offline.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# The bootstrapped runtime lands under $HOME, so build and run must agree on it.
ENV HOME=/root

# ponytail: bake the locked runtime at build time so first start needs no network
RUN python scripts/run_local_mcp.py --bootstrap-only

CMD ["python", "scripts/run_local_mcp.py"]
