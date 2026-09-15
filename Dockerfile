# Build used by Glama (glama.ai) to inspect the server in its sandbox. Hosts do not
# use this image: they run `uvx oh-my-cassette==X.Y.Z` directly.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# State lives under $HOME/.oh-my-cassette; keep it writable for any UID the
# sandbox may use.
RUN mkdir -p /data && chmod 1777 /data
ENV HOME=/data

# The server starts and lists its tools without a backend; CASSETTE_API_URL is
# only contacted when a tool is called.
CMD ["oh-my-cassette"]
