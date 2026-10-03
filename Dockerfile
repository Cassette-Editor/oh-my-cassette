# Build used by Glama (glama.ai) to inspect the server in its sandbox. Hosts do not
# use this image: they run `uvx oh-my-cassette==X.Y.Z` directly.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# The bridge starts without a reachable service and lists cassette_bridge_status,
# which reports why it is not connected. ffmpeg prepares local video before upload.
CMD ["oh-my-cassette"]
