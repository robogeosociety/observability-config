# Long-running Discord gateway bot. The MCP server + CLI run on the host, not here.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY ask_dash ./ask_dash
RUN pip install --no-cache-dir .

# Gateway bot: outbound only, no exposed port.
CMD ["python", "-m", "ask_dash.discord_bot"]
