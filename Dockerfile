# Build used by Glama (glama.ai) to inspect the server in its sandbox. Hosts do not
# use this image: they run `uvx oh-my-cassette==X.Y.Z` directly.
FROM python:3.13-slim@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# The bridge starts without a reachable service and lists cassette_bridge_status,
# which reports why it is not connected. ffmpeg prepares local video before upload.
CMD ["oh-my-cassette"]
