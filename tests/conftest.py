"""Default tests cannot contact a service or read an existing account secret."""

from pathlib import Path
import socket

import pytest

from keepflame.capabilities.secrets import FileSecretResolver


@pytest.fixture(autouse=True)
def offline_accounts_only(monkeypatch, tmp_path):
    blocked = []

    def no_network(*args, **kwargs):
        blocked.append("network")
        raise AssertionError("Tests must use a fake HTTP transport")

    original = FileSecretResolver.read_text

    def synthetic_secret(self, reference):
        if not Path(reference).resolve().is_relative_to(tmp_path.resolve()):
            blocked.append("account secret")
            raise AssertionError("Tests may read only secrets created in their tmp_path")
        return original(self, reference)

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket.socket, "sendto", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(FileSecretResolver, "read_text", synthetic_secret)
    yield
    assert not blocked, "A test attempted external I/O even if application code caught the error"
