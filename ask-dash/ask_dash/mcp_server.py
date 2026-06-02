"""MCP server — exposes the read-only tools to Claude CLI sessions over stdio.

This is the "query directly from Claude CLI" front-end. Add it to a project's
.mcp.json (see README) and any session — interactive or a scripted `claude -p`
weekly-summary job — can call list_buckets / query_influx / get_dashboard_queries
natively. No Anthropic key here: the caller's session is the model.
"""

from mcp.server.fastmcp import FastMCP

from . import tools

mcp = FastMCP("ask-dash")

# Register every read-only tool from the shared registry. FastMCP derives the
# input schema from each function's type hints.
for _name, (_fn, _schema, _desc) in tools.REGISTRY.items():
    mcp.add_tool(_fn, name=_name, description=_desc)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
