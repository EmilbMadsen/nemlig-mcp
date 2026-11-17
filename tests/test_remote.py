"""Remote mode: credentials from a file and OAuth wiring (mcp-owner-auth)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp_owner_auth")

import pyotp  # noqa: E402  (installed with mcp-owner-auth)
from mcp_owner_auth import build_app  # noqa: E402
from mcp_owner_auth.store import AuthStore  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from nemlig_mcp import client as nemlig_client  # noqa: E402
from nemlig_mcp import server as srv  # noqa: E402

PUBLIC = "https://nemlig.test"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
TOTP = pyotp.random_base32()
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def no_env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEMLIG_USER", raising=False)
    monkeypatch.delenv("NEMLIG_PASS", raising=False)


def test_credentials_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env_credentials: None
) -> None:
    path = tmp_path / "login.json"
    path.write_text(json.dumps({"username": "me@example.com", "password": "pw"}))
    path.chmod(0o600)
    monkeypatch.setenv(nemlig_client.CREDENTIALS_FILE_ENV, str(path))
    assert nemlig_client._load_credentials() == ("me@example.com", "pw")


def test_incomplete_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env_credentials: None
) -> None:
    path = tmp_path / "login.json"
    path.write_text(json.dumps({"username": "me@example.com"}))
    monkeypatch.setenv(nemlig_client.CREDENTIALS_FILE_ENV, str(path))
    with pytest.raises(nemlig_client.CredentialsError, match="username and password"):
        nemlig_client._load_credentials()


def test_readable_credentials_file_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    no_env_credentials: None,
) -> None:
    path = tmp_path / "login.json"
    path.write_text(json.dumps({"username": "me@example.com", "password": "secret-pw"}))
    path.chmod(0o644)
    monkeypatch.setenv(nemlig_client.CREDENTIALS_FILE_ENV, str(path))
    with caplog.at_level(logging.WARNING):
        nemlig_client._load_credentials()
    assert "chmod 600" in caplog.text and "secret-pw" not in caplog.text


def test_auth_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEMLIG_MCP_PUBLIC_URL", "https://n.example")
    monkeypatch.setenv(srv.STATE_DIR_ENV, str(tmp_path))
    config = srv.auth_config()
    assert config.scope == "nemlig:basket"
    assert config.resource == "https://n.example/mcp"
    assert "cannot place orders" in config.consent


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NEMLIG_MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv(srv.STATE_DIR_ENV, str(tmp_path))
    AuthStore(tmp_path / "auth.db").save_credentials(
        username="owner",
        password="a long enough password",
        totp_secret=TOTP,
        client_id="client",
        client_secret="secret",
    )
    app = build_app(srv.server, srv.auth_config())
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as test_client:
        yield test_client


def test_unauthenticated_mcp_is_401(client: TestClient) -> None:
    assert client.post("/mcp", json={}, headers=MCP_HEADERS).status_code == 401
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["scopes_supported"] == ["nemlig:basket"]


def test_oauth_flow_lists_the_tools(client: TestClient) -> None:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    authorize = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "client",
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": f"{PUBLIC}/mcp",
        },
    )
    page = client.get(authorize.headers["location"].removeprefix(PUBLIC))
    assert "nemlig.com" in page.text
    form = {
        "request": re.findall(r'name=request value="([^"]+)"', page.text)[0],
        "csrf": re.findall(r'name=csrf value="([^"]+)"', page.text)[0],
    }
    client.post(
        "/login",
        data={
            **form,
            "username": "owner",
            "password": "a long enough password",
            "totp": pyotp.TOTP(TOTP).now(),
        },
    )
    allowed = client.post("/consent", data={**form, "decision": "allow"})
    code = parse_qs(urlsplit(allowed.headers["location"]).query)["code"][0]
    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": "client",
            "client_secret": "secret",
            "code_verifier": verifier,
        },
    ).json()
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token['access_token']}"}
    listed = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers
    )
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert {"search_products", "get_basket", "set_basket_quantity"} <= names
