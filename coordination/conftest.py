def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: needs the running stack (spawns/contacts live services)")
