import socket

import pytest


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    """Phase 4.1 kill switch: every unmocked outbound socket fails immediately."""
    def blocked(*_args, **_kwargs):
        raise AssertionError("Unmocked outbound network is forbidden in Phase 4.1 tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
