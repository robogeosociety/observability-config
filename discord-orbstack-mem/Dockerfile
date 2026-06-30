# Live #ops OrbStack container-memory treemap poster. Stdlib-only (urllib) — no deps.
# Queries InfluxDB for docker_container_mem (the same data as the Grafana container
# treemap) and edits one Discord message on a loop. Outbound only, no exposed port.
FROM python:3.12-slim
WORKDIR /app
COPY orbmem.py ./
CMD ["python", "orbmem.py"]
