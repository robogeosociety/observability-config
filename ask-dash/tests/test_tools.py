"""Hermetic tests for the tool registry + dispatch (no live stack, no network).

Live data access is exercised by the README's smoke-test, not here. CI for this
repo doesn't install ask-dash's deps, so these run locally (`uv run pytest`).
"""

from ask_dash import tools


def test_anthropic_tools_shape():
    defs = tools.anthropic_tools()
    names = {d["name"] for d in defs}
    assert {"list_buckets", "query_influx", "get_dashboard_queries"} <= names
    for d in defs:
        assert d["description"]
        assert d["input_schema"]["type"] == "object"


def test_registry_callables_match_schema_required():
    # Every 'required' arg must be a real parameter of the function.
    import inspect
    for name, (fn, schema, _desc) in tools.REGISTRY.items():
        params = set(inspect.signature(fn).parameters)
        for req in schema.get("required", []):
            assert req in params, f"{name}: required '{req}' not a parameter of {fn.__name__}"


def test_dispatch_returns_error_json_not_raise(monkeypatch):
    # A tool that raises should yield an {"error": ...} JSON string, never propagate.
    import json
    monkeypatch.setitem(
        tools.REGISTRY, "list_buckets",
        (lambda: (_ for _ in ()).throw(RuntimeError("boom")),
         {"type": "object", "properties": {}, "required": []}, "x"),
    )
    out = tools.dispatch("list_buckets", {})
    assert json.loads(out)["error"].startswith("RuntimeError")


def test_dispatch_serializes_results():
    import json
    monkeypatch_val = ["a", "b"]
    tools.REGISTRY["_t"] = (lambda: monkeypatch_val,
                            {"type": "object", "properties": {}, "required": []}, "x")
    try:
        assert json.loads(tools.dispatch("_t", {})) == ["a", "b"]
    finally:
        del tools.REGISTRY["_t"]
