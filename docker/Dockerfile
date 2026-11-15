# Control-plane image: web UI + REST/WebSocket API + supervisor.
#
# Builds on the worker runtime (same Python 3.11, same wget-at environment) so it can spawn
# workers as child processes in the SAME container — one container, N projects, one web UI.
FROM carousel-base:latest

WORKDIR /opt/carousel
COPY pyproject.toml ./
COPY controlplane/ ./controlplane/
COPY web/ ./web/
# worker/ is already present from carousel-base at /opt/carousel/worker.
# --no-build-isolation: use the setuptools already in the image instead of fetching the
# build backend from PyPI at build time (that network fetch is a flaky point of failure).
RUN python3 -m pip install --no-cache-dir --no-build-isolation -e .

WORKDIR /home/warrior
EXPOSE 8001
STOPSIGNAL SIGINT
HEALTHCHECK --interval=10s --timeout=3s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/healthz', timeout=2)" || exit 1
ENTRYPOINT ["carousel"]
