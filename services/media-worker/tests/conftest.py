"""Isolate historical provider/serialization unit tests from durable dispatch."""
from contextlib import nullcontext

import pytest


@pytest.fixture
def isolated_provider_unit(monkeypatch):
    import socket
    from app import ai_execution

    def no_network(*args, **kwargs):
        pytest.fail("Provider component test attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(ai_execution, "require_pipeline", lambda *_: None)
    monkeypatch.setattr(ai_execution, "check_inputs", lambda *_: None)
    monkeypatch.setattr(ai_execution, "checkpoint_analysis", lambda *_: None)
    monkeypatch.setattr(ai_execution, "require_transport", lambda: None)
    monkeypatch.setattr(ai_execution, "attempt", lambda mode, ids, reserve=None, **_kw: nullcontext(reserve() if reserve else None))
    monkeypatch.setattr(ai_execution, "publication", nullcontext)
