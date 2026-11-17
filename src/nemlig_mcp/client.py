"""Authenticated access to the nemlig.com API via the nemlig_cli library.

Nemlig's bearer token expires after 300 seconds, so a long-lived MCP process
cannot log in once at startup. This module keeps a cached ``AuthTokens``,
refreshes it proactively before it goes stale, and retries once on a 401 in
case the server expired it early.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import nemlig_cli
import requests
from nemlig_cli import AuthTokens

from ._compat import install_default_timeout, quiet

install_default_timeout()

T = TypeVar("T")

# Nemlig issues bearer tokens with expires_in=300. Refresh early so a token
# cannot lapse mid-request.
TOKEN_TTL_SECONDS = 240

CREDENTIALS_FILE_ENV = "NEMLIG_CREDENTIALS_FILE"
_LOGGER = logging.getLogger(__name__)


class CredentialsError(RuntimeError):
    """Raised when no nemlig.com credentials could be found."""


def _load_credentials() -> tuple[str, str]:
    """Resolve credentials from the environment, then the CLI's config file.

    Environment wins so an MCP client can override per-server config without
    touching ~/.config/nemlig/login.json.
    """
    username = os.environ.get("NEMLIG_USER")
    password = os.environ.get("NEMLIG_PASS")

    # A JSON file such as a Docker secret: {"username": "...", "password": "..."}.
    credentials_file = os.environ.get(CREDENTIALS_FILE_ENV, "").strip()
    if credentials_file and not (username and password):
        path = Path(credentials_file).expanduser()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialsError(
                f"{CREDENTIALS_FILE_ENV} points to {path}, which is not a readable JSON file."
            ) from exc
        if os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            _LOGGER.warning("%s is readable by other users; restrict it with chmod 600", path)
        if isinstance(data, dict):
            username = username or data.get("username")
            password = password or data.get("password")
        if not (username and password):
            raise CredentialsError(f"{path} must contain username and password.")
        return str(username), str(password)

    if not (username and password):
        try:
            config = nemlig_cli.load_config_credentials()
        except (ValueError, OSError) as exc:
            raise CredentialsError(
                f"Could not read {nemlig_cli.CONFIG_FILE}: {exc}"
            ) from exc
        username = username or config.get("username")
        password = password or config.get("password")

    if not (username and password):
        raise CredentialsError(
            "No nemlig.com credentials found. Set NEMLIG_USER and NEMLIG_PASS, "
            f"or create {nemlig_cli.CONFIG_FILE} with "
            '{"username": "...", "password": "..."}.'
        )

    return username, password


class NemligClient:
    """Thread-safe, self-refreshing wrapper around the nemlig_cli API layer."""

    def __init__(self, username: str | None = None, password: str | None = None):
        if username and password:
            self._username, self._password = username, password
        else:
            self._username, self._password = _load_credentials()

        self._tokens: AuthTokens | None = None
        self._issued_at: float = 0.0
        self._lock = threading.Lock()

    def _fresh_tokens(self, force: bool = False) -> AuthTokens:
        with self._lock:
            expired = time.monotonic() - self._issued_at >= TOKEN_TTL_SECONDS
            if force or self._tokens is None or expired:
                with quiet():
                    self._tokens = nemlig_cli.login(self._username, self._password)
                self._issued_at = time.monotonic()
            return self._tokens

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Invoke a nemlig_cli API function, passing fresh auth as first argument.

        Retries once with a forced re-login if the API rejects the token, which
        can happen if it expired earlier than advertised.
        """
        try:
            with quiet():
                return func(self._fresh_tokens(), *args, **kwargs)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in (401, 403):
                raise
            with quiet():
                return func(self._fresh_tokens(force=True), *args, **kwargs)
