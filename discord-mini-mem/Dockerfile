# Live #ops memory-treemap poster. Stdlib-only (urllib) — no deps to install.
# Pulls the dev-status :8077/processes.json snapshot via host.docker.internal and
# edits one Discord message on a loop. Outbound only, no exposed port.
FROM python:3.12-slim

WORKDIR /app
COPY minimem.py ./

CMD ["python", "minimem.py"]
