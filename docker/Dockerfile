# Control-plane image: web UI + REST/WebSocket API + supervisor.
#
# Builds on the worker runtime so it can spawn workers as child processes in the SAME
# container (the process-per-project isolation model). One container, N projects, one
# web UI on 8001.
FROM carousel-base:latest

WORKDIR /opt/carousel
COPY --chown=warrior:warrior pyproject.toml ./
COPY --chown=warrior:warrior controlplane/ ./controlplane/
COPY --chown=warrior:warrior web/ ./web/

USER root
RUN pip install --no-cache-dir -e .
USER 1000

WORKDIR /home/warrior
EXPOSE 8001
STOPSIGNAL SIGINT
# TODO(M1): add HEALTHCHECK against http://localhost:8001 once the web app serves.
ENTRYPOINT ["carousel"]
