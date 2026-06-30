# Live #ops Claude-token heatmap poster. Stdlib-only (urllib) — no deps. Queries
# InfluxDB for the claude_code `tokens` series and edits one Discord message on a loop.
FROM python:3.12-slim
WORKDIR /app
COPY heatmap.py ./
CMD ["python", "heatmap.py"]
