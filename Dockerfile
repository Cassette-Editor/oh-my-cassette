# Build used by Glama (glama.ai) to inspect the server in its sandbox. Hosts do not
# use this image: they run `uvx oh-my-cassette==X.Y.Z` directly.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 cassette

USER cassette

# The bridge starts without a reachable service and lists cassette_bridge_status,
# which reports why it is not connected. ffmpeg prepares local video before upload.
CMD ["oh-my-cassette"]
