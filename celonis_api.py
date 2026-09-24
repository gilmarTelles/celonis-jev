"""One way to reach the tenant: the credentials, the base URL, the GET.

Every module that talks to the sandbox goes through here, so the team key is read
in one place (~/.celonis/environments.json - never in the repo) and the base URL
has one source. Nothing here writes: the reads are all GETs, and the one writer
in this repo (workbench.py) gets its headers here too.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

ENV_FILE = Path.home() / ".celonis/environments.json"

_env: dict | None = None


def env() -> dict:
    """The sandbox stanza of environments.json, read once per process."""
    global _env
    if _env is None:
        _env = json.loads(ENV_FILE.read_text())["environments"]["sandbox"]
    return _env


def base() -> str:
    return env()["url"].rstrip("/")


def headers() -> dict:
    return {"Authorization": f"Bearer {env()['api_key']}",
            "Accept": "application/json",
            "Content-Type": "application/json"}


def get(path: str, timeout: float = 60, **kw):
    r = requests.get(base() + path, headers=headers(), timeout=timeout, **kw)
    r.raise_for_status()
    return r.json()


def absolute(url: str) -> str:
    """A tenant-relative link, as a URL a browser can open."""
    return url if not url or url.startswith("http") else base() + url
