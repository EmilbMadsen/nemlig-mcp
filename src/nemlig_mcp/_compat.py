"""Compatibility shims for calling nemlig_cli from a stdio MCP server.

The CLI was written for a terminal: several of its API functions render
progress spinners with bare ``print()`` calls, and ``login()`` does so from a
background thread. On a stdio MCP server, stdout *is* the JSON-RPC transport,
so any stray byte written there corrupts the protocol stream.

``quiet()`` redirects stdout to stderr for the duration of a CLI call.
``print()`` resolves ``sys.stdout`` at call time and ``redirect_stdout``
rebinds that module attribute process-wide, so spinner threads are covered
too -- and ``Spinner.stop()`` joins its thread before returning, so nothing
escapes the context.

This shim exists so the rest of the package can call the CLI normally. If the
upstream CLI moves its progress output to stderr, delete this module and the
``quiet()`` wrappers in ``client.py``.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator

import requests


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Route anything the CLI prints to stderr, keeping stdout protocol-clean."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


# nemlig_cli calls requests without a timeout, so a stalled nemlig.com
# connection would hang a tool call forever -- and the retry logic in
# server.py, which treats timeouts as retryable, would never get the chance.
# (connect, read) seconds, applied only where the caller passed no timeout.
DEFAULT_TIMEOUT = (5, 20)

_original_request = requests.Session.request


def _request_with_default_timeout(self, method, url, **kwargs):
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = DEFAULT_TIMEOUT
    return _original_request(self, method, url, **kwargs)


def install_default_timeout() -> None:
    """Make every requests.Session in this process time out by default.

    Idempotent. Process-wide on purpose: nemlig_cli builds its own Session
    inside login(), so there is no instance to configure from outside.
    """
    requests.Session.request = _request_with_default_timeout
