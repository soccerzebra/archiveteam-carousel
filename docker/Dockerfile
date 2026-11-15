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
RUN python3 -m pip install --no-cache-dir -e .

WORKDIR /home/warrior
EXPOSE 8001
STOPSIGNAL SIGINT
HEALTHCHECK --interval=10s --timeout=3s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/api/status', timeout=2)" || exit 1
ENTRYPOINT ["carousel"]
