FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Install the locked runtime into the image's own Python. The launcher's
# default path builds a venv under $HOME instead, which needs network on
# first start and breaks in an offline inspection sandbox.
RUN pip install --no-cache-dir --requirement requirements-mcp.lock

ENV CASSETTE_MCP_SKIP_BOOTSTRAP=1

# The server creates a private 0700 config dir under $HOME at startup and
# requires it to be owned by the running user, so leave it uncreated and
# only guarantee a writable parent. Sticky-writable like /tmp, so the image
# works whether the sandbox runs it as root or as an arbitrary UID.
RUN mkdir -p /data && chmod 1777 /data
ENV HOME=/data

CMD ["python", "scripts/run_local_mcp.py"]
