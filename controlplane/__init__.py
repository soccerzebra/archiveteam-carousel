"""carousel control plane.

Greenfield supervisor + web UI + API that runs multiple ArchiveTeam projects at once,
one isolated worker process per active project. Contains no seesaw code; the seesaw API
lives only in the worker sandbox (see ``worker/`` and ``ARCHITECTURE.md``).
"""
