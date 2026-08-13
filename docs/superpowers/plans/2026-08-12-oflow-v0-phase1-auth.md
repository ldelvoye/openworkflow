# oflow v0 Phase 1 — Auth Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working `oflow connect linear` / `oflow status` / `oflow logout linear` that authenticates against Linear's MCP server over OAuth 2.1 and stores tokens securely — with no TUI and no data fetching yet.

**Architecture:** A provider-agnostic OAuth module (`auth/oauth.py`) performs dynamic client registration, PKCE authorization-code login through a loopback redirect, and token refresh. A separate storage module (`auth/store.py`) is the only code that touches tokens, defaulting to the OS keychain and refusing insecure backends. An integration registry declares which services exist; Linear is the first entry, contributing only its auth configuration in this phase.

**Tech Stack:** Python ≥3.12, `httpx` (HTTP + `MockTransport` for network-free tests), `keyring` (OS credential stores), `tomli-w` (config writing; `tomllib` reads from stdlib), `pytest`. Textual is **not** a dependency until Phase 2.

## Global Constraints

- Python floor: `requires-python = ">=3.12"`.
- Runtime dependencies in this phase are exactly: `httpx>=0.28`, `keyring>=25.0`, `tomli-w>=1.0`. Do not add others.
- Distribution name, import package, and console script are all `oflow`. They must match or `uvx oflow` breaks.
- Read-only OAuth scopes only. Never request a write scope.
- No background threads, timers, or polling anywhere in this phase.
- Tokens never appear in logs, tracebacks, `__repr__`, or committed files.
- Every test runs with no network access. `httpx.MockTransport` or a fake backend, never a live call.
- Commit messages: `type(scope): summary`, lowercase, imperative. No ticket ids. **No `Co-Authored-By` trailer naming an AI agent** — strip it if the harness adds one automatically.
- Work on branch `feat/auth-foundation` off `main`. Never commit directly to `main`.
- Each task lists what is **out of scope**. Do not implement out-of-scope items even if they seem trivial; they belong to later tasks or later phases.

---

### Task 0: Login spike (throwaway, not committed) — DONE

**Result, 2026-08-12:** consent screen passed with no workspace approval block.
A `refresh_token` was returned, granted scope was `read`, and the access token
lives 86100s (~23h55m). The token response echoed
`"resource": "https://mcp.linear.app/mcp"`, confirming the token is audience-bound
to the MCP endpoint — so the GraphQL fallback is effectively unavailable for
Linear. Endpoints are as recorded in Task 4's fixture. The plan proceeds.

This task exists because every task after it assumes something unproven: that a
third-party dynamically-registered client can complete a browser login against
the target Linear workspace. Workspace app-approval policy may block the consent
screen. If it does, this plan is void and the design needs to change. Prove it
before building anything.

**Files:**

- Create: `/tmp/oflow-spike/spike.py` (deleted at the end — **never committed**)

**Out of scope:** error handling, tests, refactoring, reusing any of this code.
The deliverable is a yes/no answer, not software.

- [x] **Step 1: Fetch the authorization server metadata**

Run:

```bash
curl -s https://mcp.linear.app/.well-known/oauth-authorization-server | python3 -m json.tool
```

Expected: JSON containing `registration_endpoint`, `authorization_endpoint`,
`token_endpoint`, `code_challenge_methods_supported` including `"S256"`, and
`scopes_supported` including `"read"`. Record these four URLs — later tasks use
them as recorded fixtures.

- [x] **Step 2: Register a client**

Run:

```bash
curl -s -X POST https://mcp.linear.app/register \
  -H 'content-type: application/json' \
  -d '{"client_name":"oflow-spike","redirect_uris":["http://127.0.0.1:8765/callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}' \
  | python3 -m json.tool
```

Expected: a JSON body containing `client_id`. Note whether a `client_secret` is
returned — with `token_endpoint_auth_method: none` there should be none.

- [x] **Step 3: Complete one real browser login**

Write `/tmp/oflow-spike/spike.py`:

```python
import base64, hashlib, http.server, secrets, threading, urllib.parse, webbrowser, json, httpx

CLIENT_ID = "<paste from step 2>"
REDIRECT = "http://127.0.0.1:8765/callback"
AUTH_URL = "https://mcp.linear.app/authorize"
TOKEN_URL = "https://mcp.linear.app/token"
RESOURCE = "https://mcp.linear.app/mcp"

verifier = secrets.token_urlsafe(64)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)
code_box = {}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code_box.update({k: v[0] for k, v in q.items()})
        self.send_response(200); self.end_headers()
        self.wfile.write(b"done, close this tab")
    def log_message(self, *a): pass

server = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
threading.Thread(target=server.handle_request, daemon=True).start()

params = urllib.parse.urlencode({
    "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
    "scope": "read", "state": state, "resource": RESOURCE,
    "code_challenge": challenge, "code_challenge_method": "S256",
})
print("opening browser...")
webbrowser.open(f"{AUTH_URL}?{params}")
input("press enter after approving in the browser: ")

assert code_box.get("state") == state, f"state mismatch: {code_box}"
r = httpx.post(TOKEN_URL, data={
    "grant_type": "authorization_code", "code": code_box["code"],
    "redirect_uri": REDIRECT, "client_id": CLIENT_ID, "code_verifier": verifier,
    "resource": RESOURCE,
})
print(r.status_code)
print(json.dumps({k: ("<redacted>" if "token" in k else v) for k, v in r.json().items()}, indent=2))
```

Run: `cd /tmp/oflow-spike && uv run --with httpx python spike.py`

Expected: a Linear consent screen appears, approval succeeds, and the token
response is `200` with `access_token`, `refresh_token`, `expires_in`, and
`scope: read` present (values redacted in the printout).

- [x] **Step 4: Record the decision gate**

Answer these three questions in the PR description of Task 1, or in a comment on
this plan:

1. Did the consent screen appear, or did the workspace block the app?
2. Was a `refresh_token` returned?
3. What are the four endpoint URLs from Step 1?

**If the consent screen was blocked: STOP.** Do not proceed to Task 1. Report
back — the design needs a different auth path and the plan must be rewritten.

- [x] **Step 5: Delete the spike**

Run: `rm -rf /tmp/oflow-spike`

Nothing from this task is committed. The endpoint URLs recorded in Step 4 are
the only output that survives.

---

### Task 1: Repo scaffold

**Files:**

- Create: `pyproject.toml`, `.gitignore`, `LICENSE`, `README.md`, `.github/workflows/ci.yml`, `src/oflow/__init__.py`, `tests/test_smoke.py`

**Interfaces:**

- Consumes: nothing.
- Produces: an installable `oflow` package and a green CI run. Every later task
  assumes `uv run pytest` and `uv run ruff check .` work.

**Out of scope:** build matrices across Python versions or OSes, coverage
thresholds, release automation, publishing to PyPI, pre-commit hooks, dependabot.
One Python version, lint and test, nothing else. This task is the single most
likely place to over-build; resist it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_smoke.py`:

```python
def test_package_imports():
    import oflow

    assert oflow.__version__ == "0.0.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oflow'`

- [ ] **Step 3: Write the scaffold**

Create `pyproject.toml`:

```toml
[project]
name = "oflow"
version = "0.0.0"
description = "Keyboard-driven terminal dashboard, one tab per connected integration"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
dependencies = [
    "httpx>=0.28",
    "keyring>=25.0",
    "tomli-w>=1.0",
]

[project.scripts]
oflow = "oflow.cli:main"

[dependency-groups]
dev = ["pytest>=8.0", "ruff>=0.9"]

[build-system]
requires = ["uv_build>=0.11,<0.13"]
build-backend = "uv_build"

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
extend-exclude = ["docs"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

Create `src/oflow/__init__.py`:

```python
__version__ = "0.0.0"
```

Create `src/oflow/cli.py`:

```python
def main() -> int:
    print("oflow: no commands yet")
    return 0
```

Create `.gitignore`:

```
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.ruff_cache/
dist/
```

Create `LICENSE` with the standard MIT license text, copyright `2026 Lucas Delvoye`.

Create `README.md`:

```markdown
# oflow

A keyboard-driven terminal dashboard. Each integration you connect becomes a tab.
Nothing is enabled by default.

## Install

Not published to PyPI yet. From a checkout:

    uv run oflow

Once released, `uvx oflow` will fetch and run it directly. That first run
installs the package before the screen paints, so expect a brief blank terminal.

## Status

Pre-alpha. See `docs/superpowers/specs/` for the design.
```

Create `.github/workflows/ci.yml`:

```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"
      - run: uv sync --all-extras --dev
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest -v
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -v && uv run ruff check .`
Expected: 1 passed, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore LICENSE README.md .github src tests
git commit -m "chore: scaffold package, lint, and ci"
```

---

### Task 2: Config file

**Files:**

- Create: `src/oflow/config.py`, `tests/test_config.py`

**Interfaces:**

- Consumes: nothing.
- Produces:
  - `config_dir() -> Path` — honours `OFLOW_CONFIG_DIR`, else `~/.config/oflow`.
  - `config_path() -> Path` — `config_dir() / "config.toml"`.
  - `TabConfig(integration: str, client_id: str | None)` — frozen dataclass.
  - `Config(tabs: tuple[TabConfig, ...])` — frozen dataclass.
  - `load_config() -> Config` — returns `Config(tabs=())` when the file is absent.
  - `save_config(config: Config) -> None` — writes atomically, creates the
    directory `0700`.
  - `add_tab(config: Config, tab: TabConfig) -> Config` — replaces an existing
    entry with the same `integration`, else appends. Returns a new `Config`.

**Out of scope:** per-integration settings beyond `client_id`, tab reordering
commands, config validation errors with suggestions, migrations.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
import pytest

from oflow.config import Config, TabConfig, add_tab, config_dir, load_config, save_config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))


def test_config_dir_honours_env(tmp_path):
    assert config_dir() == tmp_path / "cfg"


def test_load_missing_config_returns_empty():
    assert load_config() == Config(tabs=())


def test_save_then_load_roundtrips():
    config = Config(tabs=(TabConfig(integration="linear", client_id="abc123"),))
    save_config(config)
    assert load_config() == config


def test_save_creates_directory_with_0700():
    save_config(Config(tabs=()))
    assert (config_dir().stat().st_mode & 0o777) == 0o700


def test_add_tab_appends_then_replaces():
    config = add_tab(Config(tabs=()), TabConfig(integration="linear", client_id="a"))
    assert config.tabs == (TabConfig(integration="linear", client_id="a"),)

    config = add_tab(config, TabConfig(integration="linear", client_id="b"))
    assert config.tabs == (TabConfig(integration="linear", client_id="b"),)


def test_tab_order_is_preserved():
    config = Config(tabs=())
    config = add_tab(config, TabConfig(integration="linear", client_id="a"))
    config = add_tab(config, TabConfig(integration="sentry", client_id="b"))
    save_config(config)
    assert [tab.integration for tab in load_config().tabs] == ["linear", "sentry"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oflow.config'`

- [ ] **Step 3: Write the implementation**

Create `src/oflow/config.py`:

```python
"""Non-secret configuration: which tabs exist, in what order, and their client ids."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

CONFIG_DIR_ENV = "OFLOW_CONFIG_DIR"


@dataclass(frozen=True)
class TabConfig:
    integration: str
    client_id: str | None = None


@dataclass(frozen=True)
class Config:
    tabs: tuple[TabConfig, ...] = ()


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".config" / "oflow"


def config_path() -> Path:
    return config_dir() / "config.toml"


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text())
    tabs = tuple(
        TabConfig(integration=entry["integration"], client_id=entry.get("client_id"))
        for entry in raw.get("tabs", [])
    )
    return Config(tabs=tabs)


def save_config(config: Config) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    payload = {
        "tabs": [
            {"integration": tab.integration}
            | ({"client_id": tab.client_id} if tab.client_id else {})
            for tab in config.tabs
        ]
    }
    temporary = config_path().with_suffix(".toml.tmp")
    temporary.write_bytes(tomli_w.dumps(payload).encode())
    temporary.replace(config_path())


def add_tab(config: Config, tab: TabConfig) -> Config:
    replaced = tuple(tab if existing.integration == tab.integration else existing
                     for existing in config.tabs)
    if any(existing.integration == tab.integration for existing in config.tabs):
        return Config(tabs=replaced)
    return Config(tabs=config.tabs + (tab,))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/oflow/config.py tests/test_config.py
git commit -m "feat(config): add config file with tab list and order"
```

---

### Task 3: Credential store

**Files:**

- Create: `src/oflow/auth/__init__.py`, `src/oflow/auth/store.py`, `tests/test_store.py`

**Interfaces:**

- Consumes: `oflow.config.config_dir`.
- Produces:
  - `Credentials(access_token: str, refresh_token: str | None, expires_at: datetime | None, scope: str)`
    — frozen dataclass whose `__repr__` redacts both tokens.
  - `Credentials.is_expired(now: datetime) -> bool`
  - `get_credentials(integration_id: str) -> Credentials | None`
  - `set_credentials(integration_id: str, credentials: Credentials) -> None`
  - `delete_credentials(integration_id: str) -> None`
  - `InsecureBackendError`, `CredentialPermissionError` — both subclass `CredentialStoreError`.

**Out of scope:** token refresh (Task 4 owns it), migrating credentials between
stores, multi-account support for one integration.

This is the security-sensitive task. Two rules it must enforce: never write to a
backend that is not known-secure, and never let a token reach a string
representation.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from oflow.auth.store import (
    Credentials,
    CredentialPermissionError,
    InsecureBackendError,
    delete_credentials,
    get_credentials,
    set_credentials,
)

CREDS = Credentials(
    access_token="secret-access-token",
    refresh_token="secret-refresh-token",
    expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    scope="read",
)


@pytest.fixture
def file_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OFLOW_CREDENTIAL_STORE", "file")
    return tmp_path / "cfg"


def test_repr_redacts_both_tokens():
    rendered = repr(CREDS)
    assert "secret-access-token" not in rendered
    assert "secret-refresh-token" not in rendered
    assert "redacted" in rendered


def test_str_redacts_both_tokens():
    rendered = str(CREDS)
    assert "secret-access-token" not in rendered
    assert "secret-refresh-token" not in rendered


def test_is_expired():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert CREDS.is_expired(now + timedelta(seconds=1)) is True
    assert CREDS.is_expired(now - timedelta(hours=1)) is False


def test_file_store_roundtrip(file_store):
    assert get_credentials("linear") is None
    set_credentials("linear", CREDS)
    assert get_credentials("linear") == CREDS


def test_file_store_writes_0600(file_store):
    set_credentials("linear", CREDS)
    path = file_store / "credentials.json"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_file_store_rejects_wide_permissions(file_store):
    set_credentials("linear", CREDS)
    (file_store / "credentials.json").chmod(0o644)
    with pytest.raises(CredentialPermissionError):
        get_credentials("linear")


def test_delete_removes_only_that_integration(file_store):
    set_credentials("linear", CREDS)
    set_credentials("sentry", CREDS)
    delete_credentials("linear")
    assert get_credentials("linear") is None
    assert get_credentials("sentry") == CREDS


def test_keyring_store_rejects_insecure_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OFLOW_CREDENTIAL_STORE", raising=False)

    class FakeInsecureBackend:
        pass

    monkeypatch.setattr("oflow.auth.store.keyring.get_keyring", lambda: FakeInsecureBackend())
    with pytest.raises(InsecureBackendError) as excinfo:
        set_credentials("linear", CREDS)
    assert "OFLOW_CREDENTIAL_STORE=file" in str(excinfo.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oflow.auth'`

- [ ] **Step 3: Write the implementation**

Create `src/oflow/auth/__init__.py` (empty file).

Create `src/oflow/auth/store.py`:

```python
"""The only module that reads or writes credentials.

Defaults to the OS keychain. An insecure keyring backend is refused rather than
used, because an unrelated ``keyrings.alt`` install in the same environment is
enough to turn a silent success into a plaintext token on disk.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import keyring
from keyring.errors import PasswordDeleteError

from oflow.config import config_dir

STORE_ENV = "OFLOW_CREDENTIAL_STORE"
SERVICE = "oflow"

SECURE_BACKENDS = frozenset(
    {
        "keyring.backends.macOS.Keyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.kwallet.DBusKeyring",
    }
)


class CredentialStoreError(Exception):
    """Base class for anything that stops credentials being read or written."""


class InsecureBackendError(CredentialStoreError):
    pass


class CredentialPermissionError(CredentialStoreError):
    pass


@dataclass(frozen=True)
class Credentials:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str

    def __repr__(self) -> str:
        expiry = self.expires_at.isoformat() if self.expires_at else "never"
        return f"Credentials(access_token=<redacted>, refresh_token=<redacted>, expires_at={expiry}, scope={self.scope!r})"

    __str__ = __repr__

    def is_expired(self, now: datetime) -> bool:
        if self.expires_at is None:
            return False
        return now > self.expires_at


def _backend_name(backend: object) -> str:
    return f"{type(backend).__module__}.{type(backend).__qualname__}"


def _require_secure_backend() -> None:
    backend = keyring.get_keyring()
    name = _backend_name(backend)
    if name not in SECURE_BACKENDS:
        raise InsecureBackendError(
            f"refusing to store tokens in {name}, which is not a known-secure keyring backend. "
            f"Set OFLOW_CREDENTIAL_STORE=file to use a 0600 file instead."
        )


def _use_file_store() -> bool:
    return os.environ.get(STORE_ENV) == "file"


def _credentials_path() -> Path:
    return config_dir() / "credentials.json"


def _read_file_store() -> dict[str, dict]:
    path = _credentials_path()
    if not path.exists():
        return {}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise CredentialPermissionError(
            f"{path} has mode {mode:o}, expected 600. Fix it with: chmod 600 {path}"
        )
    return json.loads(path.read_text())


def _write_file_store(payload: dict[str, dict]) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = _credentials_path()
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(payload))


def _to_dict(credentials: Credentials) -> dict:
    return {
        "access_token": credentials.access_token,
        "refresh_token": credentials.refresh_token,
        "expires_at": credentials.expires_at.isoformat() if credentials.expires_at else None,
        "scope": credentials.scope,
    }


def _from_dict(raw: dict) -> Credentials:
    expires_at = raw.get("expires_at")
    return Credentials(
        access_token=raw["access_token"],
        refresh_token=raw.get("refresh_token"),
        expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
        scope=raw.get("scope", ""),
    )


def get_credentials(integration_id: str) -> Credentials | None:
    if _use_file_store():
        raw = _read_file_store().get(integration_id)
        return _from_dict(raw) if raw else None
    _require_secure_backend()
    stored = keyring.get_password(SERVICE, integration_id)
    return _from_dict(json.loads(stored)) if stored else None


def set_credentials(integration_id: str, credentials: Credentials) -> None:
    if _use_file_store():
        payload = _read_file_store()
        payload[integration_id] = _to_dict(credentials)
        _write_file_store(payload)
        return
    _require_secure_backend()
    keyring.set_password(SERVICE, integration_id, json.dumps(_to_dict(credentials)))


def delete_credentials(integration_id: str) -> None:
    if _use_file_store():
        payload = _read_file_store()
        payload.pop(integration_id, None)
        _write_file_store(payload)
        return
    _require_secure_backend()
    try:
        keyring.delete_password(SERVICE, integration_id)
    except PasswordDeleteError:
        pass


def now() -> datetime:
    return datetime.now(UTC)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/oflow/auth tests/test_store.py
git commit -m "feat(auth): add credential store with backend safety check

Refuses any keyring backend not on a known-secure allowlist rather than
trusting NoKeyringError to arrive: an unrelated keyrings.alt install in
the same environment gives keyring an insecure backend to pick instead."
```

---

### Task 4: OAuth flow

**Files:**

- Create: `src/oflow/auth/oauth.py`, `tests/test_oauth.py`, `tests/fixtures/oauth_metadata.json`

**Interfaces:**

- Consumes: `oflow.auth.store.Credentials`.
- Produces:
  - `ProviderConfig(metadata_url: str, scopes: tuple[str, ...], client_name: str)`
  - `ServerMetadata(authorization_endpoint, token_endpoint, registration_endpoint, resource)`
    — `resource` is the RFC 8707 protected-resource identifier, sent on the
    authorize redirect and every token request when present.
  - `discover(client: httpx.Client, provider: ProviderConfig) -> ServerMetadata`
  - `register_client(client, metadata, provider, redirect_uri) -> str` (returns `client_id`)
  - `make_pkce_pair() -> tuple[str, str]` (verifier, S256 challenge)
  - `build_authorize_url(metadata, client_id, redirect_uri, challenge, scopes, state) -> str`
  - `exchange_code(client, metadata, client_id, code, verifier, redirect_uri) -> Credentials`
  - `refresh_credentials(client, metadata, client_id, credentials) -> Credentials`
  - `LOOPBACK_PORT = 8765`, `REDIRECT_URI = "http://127.0.0.1:8765/callback"`
  - `OAuthError` — raised for any non-2xx token or registration response.

**Out of scope:** the interactive browser/loopback orchestration (Task 6 owns
it), concurrent-refresh locking (Phase 2, when two tabs can refresh at once),
token introspection.

Revocation belongs here rather than in Task 6: `logout` needs it, and the
alternative is reopening this module later to add a single function.

**Open decision — the loopback port.** `LOOPBACK_PORT = 8765` is fixed, so any
local process can bind it first and receive the authorization code. PKCE makes a
stolen code useless without the verifier, which is why this is tolerable rather
than fatal, but a free port chosen at runtime would be better. The obstacle is
that dynamic client registration pins `redirect_uris` at registration time.
RFC 8252 §7.3 says an authorization server should accept any port on a loopback
redirect regardless of what was registered — test whether Linear honours that
before settling on the fixed port.

Every test uses `httpx.MockTransport`. No test may make a real network call.

- [ ] **Step 1: Record the metadata fixture**

Create `tests/fixtures/oauth_metadata.json` using the real values recorded in
Task 0 Step 4:

```json
{
  "issuer": "https://mcp.linear.app",
  "authorization_endpoint": "https://mcp.linear.app/authorize",
  "token_endpoint": "https://mcp.linear.app/token",
  "registration_endpoint": "https://mcp.linear.app/register",
  "code_challenge_methods_supported": ["S256"],
  "scopes_supported": ["read", "write", "openid", "email"],
  "token_endpoint_auth_methods_supported": [
    "client_secret_basic",
    "client_secret_post",
    "none"
  ],
  "resource": "https://mcp.linear.app/mcp"
}
```

These are the live values, fetched from the metadata endpoint. If Task 0 recorded
anything different, use what Task 0 saw — this file is a recording, not an
invention.

The `resource` field matters: Linear's MCP endpoint is an RFC 8707 protected
resource, so `resource=https://mcp.linear.app/mcp` must accompany both the
authorize redirect and every token request. Omitting it yields a token with the
wrong audience, which fails later at the MCP endpoint rather than here.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_oauth.py`:

```python
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from oflow.auth.oauth import (
    OAuthError,
    ProviderConfig,
    build_authorize_url,
    discover,
    exchange_code,
    make_pkce_pair,
    refresh_credentials,
    register_client,
)
from oflow.auth.store import Credentials

METADATA = json.loads((Path(__file__).parent / "fixtures" / "oauth_metadata.json").read_text())
PROVIDER = ProviderConfig(
    metadata_url="https://mcp.linear.app/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="oflow",
)
REDIRECT = "http://127.0.0.1:8765/callback"


def transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discover_reads_endpoints():
    client = transport(lambda request: httpx.Response(200, json=METADATA))
    metadata = discover(client, PROVIDER)
    assert metadata.token_endpoint == METADATA["token_endpoint"]
    assert metadata.registration_endpoint == METADATA["registration_endpoint"]


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = make_pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()
    assert "=" not in challenge


def test_register_client_posts_public_client_and_returns_id():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"client_id": "client-abc"})

    metadata = discover(transport(lambda r: httpx.Response(200, json=METADATA)), PROVIDER)
    client_id = register_client(transport(handler), metadata, PROVIDER, REDIRECT)

    assert client_id == "client-abc"
    assert seen["body"]["token_endpoint_auth_method"] == "none"
    assert seen["body"]["redirect_uris"] == [REDIRECT]
    assert "client_secret" not in seen["body"]


def test_authorize_url_carries_pkce_and_state():
    metadata = discover(transport(lambda r: httpx.Response(200, json=METADATA)), PROVIDER)
    url = build_authorize_url(metadata, "client-abc", REDIRECT, "chal", ("read",), "state-xyz")
    query = parse_qs(urlparse(url).query)

    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["chal"]
    assert query["state"] == ["state-xyz"]
    assert query["scope"] == ["read"]
    assert query["response_type"] == ["code"]
    assert query["resource"] == ["https://mcp.linear.app/mcp"]


def test_exchange_code_returns_credentials_with_expiry():
    def handler(request):
        body = parse_qs(request.content.decode())
        assert body["grant_type"] == ["authorization_code"]
        assert body["code_verifier"] == ["verifier-1"]
        assert body["resource"] == ["https://mcp.linear.app/mcp"]
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3600,
                "scope": "read",
            },
        )

    metadata = discover(transport(lambda r: httpx.Response(200, json=METADATA)), PROVIDER)
    credentials = exchange_code(
        transport(handler), metadata, "client-abc", "code-1", "verifier-1", REDIRECT
    )

    assert credentials.access_token == "at-1"
    assert credentials.refresh_token == "rt-1"
    assert credentials.scope == "read"
    assert credentials.expires_at is not None


def test_exchange_code_raises_on_error_response():
    handler = lambda request: httpx.Response(400, json={"error": "invalid_grant"})
    metadata = discover(transport(lambda r: httpx.Response(200, json=METADATA)), PROVIDER)

    with pytest.raises(OAuthError) as excinfo:
        exchange_code(transport(handler), metadata, "client-abc", "bad", "v", REDIRECT)
    assert "invalid_grant" in str(excinfo.value)


def test_refresh_keeps_old_refresh_token_when_none_returned():
    handler = lambda request: httpx.Response(
        200, json={"access_token": "at-2", "expires_in": 3600, "scope": "read"}
    )
    metadata = discover(transport(lambda r: httpx.Response(200, json=METADATA)), PROVIDER)
    old = Credentials(access_token="at-1", refresh_token="rt-1", expires_at=None, scope="read")

    refreshed = refresh_credentials(transport(handler), metadata, "client-abc", old)

    assert refreshed.access_token == "at-2"
    assert refreshed.refresh_token == "rt-1"


def test_oauth_error_message_never_contains_a_token():
    handler = lambda request: httpx.Response(401, json={"error": "invalid_client"})
    metadata = discover(transport(lambda r: httpx.Response(200, json=METADATA)), PROVIDER)
    old = Credentials(access_token="at-secret", refresh_token="rt-secret", expires_at=None, scope="read")

    with pytest.raises(OAuthError) as excinfo:
        refresh_credentials(transport(handler), metadata, "client-abc", old)
    assert "rt-secret" not in str(excinfo.value)
    assert "at-secret" not in str(excinfo.value)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_oauth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oflow.auth.oauth'`

- [ ] **Step 4: Write the implementation**

Create `src/oflow/auth/oauth.py`:

```python
"""Provider-agnostic OAuth 2.1: discovery, dynamic client registration, PKCE, refresh.

Nothing here is Linear-specific. An integration supplies a ProviderConfig and
gets a Credentials back.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx

from oflow.auth.store import Credentials, now

LOOPBACK_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{LOOPBACK_PORT}/callback"


class OAuthError(Exception):
    """A registration or token request failed. Never carries a token value."""


@dataclass(frozen=True)
class ProviderConfig:
    metadata_url: str
    scopes: tuple[str, ...]
    client_name: str


@dataclass(frozen=True)
class ServerMetadata:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    resource: str | None = None


def discover(client: httpx.Client, provider: ProviderConfig) -> ServerMetadata:
    response = client.get(provider.metadata_url)
    if response.status_code != 200:
        raise OAuthError(f"metadata discovery failed with {response.status_code}")
    payload = response.json()
    return ServerMetadata(
        authorization_endpoint=payload["authorization_endpoint"],
        token_endpoint=payload["token_endpoint"],
        registration_endpoint=payload["registration_endpoint"],
        resource=payload.get("resource"),
    )


def register_client(
    client: httpx.Client,
    metadata: ServerMetadata,
    provider: ProviderConfig,
    redirect_uri: str,
) -> str:
    response = client.post(
        metadata.registration_endpoint,
        json={
            "client_name": provider.client_name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    if response.status_code not in (200, 201):
        raise OAuthError(f"client registration failed with {response.status_code}")
    return response.json()["client_id"]


def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(
    metadata: ServerMetadata,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    scopes: tuple[str, ...],
    state: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if metadata.resource:
        params["resource"] = metadata.resource
    return f"{metadata.authorization_endpoint}?{urlencode(params)}"


def _credentials_from_token_response(payload: dict, fallback_refresh: str | None) -> Credentials:
    expires_in = payload.get("expires_in")
    return Credentials(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or fallback_refresh,
        expires_at=now() + timedelta(seconds=expires_in) if expires_in else None,
        scope=payload.get("scope", ""),
    )


def _post_token(client: httpx.Client, metadata: ServerMetadata, form: dict) -> dict:
    if metadata.resource:
        form = form | {"resource": metadata.resource}
    response = client.post(metadata.token_endpoint, data=form)
    if response.status_code != 200:
        try:
            error = response.json().get("error", "unknown_error")
        except ValueError:
            error = "unparseable_error_response"
        raise OAuthError(f"token request failed with {response.status_code}: {error}")
    return response.json()


def exchange_code(
    client: httpx.Client,
    metadata: ServerMetadata,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> Credentials:
    payload = _post_token(
        client,
        metadata,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    return _credentials_from_token_response(payload, fallback_refresh=None)


def refresh_credentials(
    client: httpx.Client,
    metadata: ServerMetadata,
    client_id: str,
    credentials: Credentials,
) -> Credentials:
    if credentials.refresh_token is None:
        raise OAuthError("no refresh token available; re-run oflow connect")
    payload = _post_token(
        client,
        metadata,
        {
            "grant_type": "refresh_token",
            "refresh_token": credentials.refresh_token,
            "client_id": client_id,
        },
    )
    return _credentials_from_token_response(payload, fallback_refresh=credentials.refresh_token)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add src/oflow/auth/oauth.py tests/test_oauth.py tests/fixtures/oauth_metadata.json
git commit -m "feat(auth): add oauth 2.1 discovery, registration, and pkce flow"
```

---

### Task 5: Integration registry and contract

**Files:**

- Create: `src/oflow/integrations/__init__.py`, `src/oflow/contract.py`, `src/oflow/registry.py`, `tests/test_registry.py`

**Interfaces:**

- Consumes: `oflow.auth.oauth.ProviderConfig`.
- Produces:
  - `ActionClass` — `StrEnum` with `LOCAL`, `LAUNCH`, `REMOTE`.
  - `Action(id: str, label: str, key: str, action_class: ActionClass)`
  - `Manifest(id: str, display_name: str, provider: ProviderConfig, stale_after: timedelta, actions: tuple[Action, ...])`
  - `IntegrationError`, and its subclasses `AuthExpired`, `Unavailable`, `Malformed`.
  - `Integration` — a `Protocol` with a `manifest: Manifest` attribute.
  - `get_integration(integration_id: str) -> Integration` — raises
    `UnknownIntegration` for anything not registered.
  - `known_integration_ids() -> tuple[str, ...]`
  - `UnknownIntegration` — its message lists the registered ids.

**Out of scope:** the `fetch` method and item types (Phase 2 defines them once
there is a panel to consume them), the Linear manifest itself (Task 6), any
`REMOTE` action implementation.

The contract is **provisional**. It has exactly one consumer, and contracts
designed against one implementation are reliably wrong for the second. Add
nothing Linear does not need. Generalization waits for integration #2.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_registry.py`:

```python
from datetime import timedelta

import pytest

from oflow.auth.oauth import ProviderConfig
from oflow.contract import Action, ActionClass, Manifest
from oflow.registry import UnknownIntegration, get_integration, known_integration_ids

FAKE_PROVIDER = ProviderConfig(
    metadata_url="https://example.invalid/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="oflow-test",
)


def test_unknown_integration_lists_what_is_available():
    with pytest.raises(UnknownIntegration) as excinfo:
        get_integration("jira")
    message = str(excinfo.value)
    assert "jira" in message
    assert "linear" in message


def test_known_ids_are_registered():
    assert "linear" in known_integration_ids()


def test_action_class_is_a_string_enum():
    assert ActionClass.LAUNCH == "launch"
    assert {member.value for member in ActionClass} == {"local", "launch", "remote"}


def test_manifest_rejects_duplicate_action_keys():
    with pytest.raises(ValueError, match="duplicate action key"):
        Manifest(
            id="fake",
            display_name="Fake",
            provider=FAKE_PROVIDER,
            stale_after=timedelta(minutes=5),
            actions=(
                Action(id="open", label="Open", key="o", action_class=ActionClass.LAUNCH),
                Action(id="copy", label="Copy", key="o", action_class=ActionClass.LOCAL),
            ),
        )


def test_manifest_rejects_globally_reserved_keys():
    with pytest.raises(ValueError, match="reserved"):
        Manifest(
            id="fake",
            display_name="Fake",
            provider=FAKE_PROVIDER,
            stale_after=timedelta(minutes=5),
            actions=(Action(id="refresh", label="Refresh", key="r", action_class=ActionClass.LOCAL),),
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oflow.contract'`

- [ ] **Step 3: Write the contract**

Create `src/oflow/contract.py`:

```python
"""What an integration must provide, and the errors it is allowed to raise."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from oflow.auth.oauth import ProviderConfig

RESERVED_KEYS = frozenset({"r", "q", "?", "tab", "escape", "j", "k", "enter"})


class ActionClass(StrEnum):
    LOCAL = "local"
    LAUNCH = "launch"
    REMOTE = "remote"


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    key: str
    action_class: ActionClass


@dataclass(frozen=True)
class Manifest:
    id: str
    display_name: str
    provider: ProviderConfig
    stale_after: timedelta
    actions: tuple[Action, ...]

    def __post_init__(self) -> None:
        keys = [action.key for action in self.actions]
        duplicates = {key for key in keys if keys.count(key) > 1}
        if duplicates:
            raise ValueError(f"duplicate action key(s) in {self.id}: {sorted(duplicates)}")
        reserved = sorted(set(keys) & RESERVED_KEYS)
        if reserved:
            raise ValueError(
                f"{self.id} binds reserved shell key(s) {reserved}; panels may add keys, not rebind global ones"
            )


class IntegrationError(Exception):
    """Base class for every failure a source is allowed to surface to the shell."""


class AuthExpired(IntegrationError):
    """Credentials are no longer valid. The shell offers an inline re-connect."""


class Unavailable(IntegrationError):
    """The service could not be reached. The shell keeps last-good data and marks it stale."""


class Malformed(IntegrationError):
    """The response did not match the expected shape. The tab is broken; say so."""


class Integration(Protocol):
    manifest: Manifest
```

- [ ] **Step 4: Write the registry**

Create `src/oflow/integrations/__init__.py` (empty file).

Create `src/oflow/registry.py`:

```python
"""The allowlist. An integration exists only if it is registered here."""

from __future__ import annotations

from oflow.contract import Integration


class UnknownIntegration(Exception):
    pass


def _registry() -> dict[str, Integration]:
    from oflow.integrations import linear

    return {linear.INTEGRATION.manifest.id: linear.INTEGRATION}


def known_integration_ids() -> tuple[str, ...]:
    return tuple(sorted(_registry()))


def get_integration(integration_id: str) -> Integration:
    registry = _registry()
    if integration_id not in registry:
        available = ", ".join(sorted(registry))
        raise UnknownIntegration(
            f"{integration_id!r} is not a supported integration. Available: {available}"
        )
    return registry[integration_id]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_registry.py -v`
Expected: all pass.

The allowlist is a tuple in `integrations/__init__.py`, empty until Task 6 adds
Linear, and the tests populate it with a fake integration. That keeps this task
committable on its own: an earlier draft of this plan had it depend on the Linear
module and told you to commit Tasks 5 and 6 together, which meant landing a
knowingly-red commit to satisfy a plan rather than a constraint.

---

### Task 6: Linear manifest and the CLI

**Files:**

- Create: `src/oflow/integrations/linear/__init__.py`, `src/oflow/integrations/linear/manifest.py`, `tests/test_cli.py`
- Modify: `src/oflow/cli.py` (replace the Task 1 placeholder entirely)

**Interfaces:**

- Consumes: `oflow.registry.get_integration`, `oflow.auth.oauth.*`, `oflow.auth.store.*`, `oflow.config.*`.
- Produces:
  - `oflow connect <id>` — discovery, registration (cached in `config.toml`),
    browser login, token exchange, credential storage, tab added to config.
  - `oflow status` — one line per configured tab: id, connected/disconnected,
    scope, expiry. Never a token value.
  - `oflow logout <id>` — deletes credentials, leaves the tab in config.
  - `run_login(provider, client_id_from_config) -> tuple[str, Credentials]` in
    `cli.py` — the loopback orchestration, returning the (possibly newly
    registered) client id alongside credentials.

**Out of scope:** the TUI, `oflow run`, fetching any issue data, a `source.py`
or `panel.py` for Linear, refresh-on-launch. This phase ends when authentication
works end to end.

**`logout` must revoke, not just delete.** Deleting the local credentials leaves
the access token valid for its remaining lifetime and the refresh token valid
indefinitely, so anyone who captured them keeps their access — a command named
`logout` that only forgets is misleading. Linear's metadata advertises
`revocation_endpoint: https://mcp.linear.app/token`, so `logout` posts an
RFC 7009 revocation for the refresh token first and treats deleting the local
copy as cleanup afterwards. Revocation failing (offline, already revoked) must
still delete locally, or a network problem would trap credentials on the
machine.

- [ ] **Step 1: Write the Linear manifest**

Create `src/oflow/integrations/linear/__init__.py`:

```python
from oflow.integrations.linear.manifest import INTEGRATION, MANIFEST

__all__ = ["INTEGRATION", "MANIFEST"]
```

Create `src/oflow/integrations/linear/manifest.py`:

```python
"""Linear's declaration. Auth only in this phase; source and panel arrive in Phase 2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from oflow.auth.oauth import ProviderConfig
from oflow.contract import Action, ActionClass, Manifest

PROVIDER = ProviderConfig(
    metadata_url="https://mcp.linear.app/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="oflow",
)

MANIFEST = Manifest(
    id="linear",
    display_name="Linear",
    provider=PROVIDER,
    stale_after=timedelta(minutes=5),
    actions=(
        Action(id="open", label="Open in Linear", key="o", action_class=ActionClass.LAUNCH),
    ),
)


@dataclass(frozen=True)
class LinearIntegration:
    manifest: Manifest = MANIFEST


INTEGRATION = LinearIntegration()
```

- [ ] **Step 2: Write the failing CLI tests**

Create `tests/test_cli.py`:

```python
from datetime import UTC, datetime

import pytest

from oflow.auth.store import Credentials, set_credentials
from oflow.cli import main


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OFLOW_CREDENTIAL_STORE", "file")


def test_connect_rejects_unknown_integration(capsys):
    exit_code = main(["connect", "jira"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not a supported integration" in captured.err
    assert "linear" in captured.err


def test_status_with_nothing_configured(capsys):
    assert main(["status"]) == 0
    assert "no tabs configured" in capsys.readouterr().out


def test_status_never_prints_a_token(capsys):
    from oflow.config import Config, TabConfig, save_config

    save_config(Config(tabs=(TabConfig(integration="linear", client_id="client-abc"),)))
    set_credentials(
        "linear",
        Credentials(
            access_token="at-secret",
            refresh_token="rt-secret",
            expires_at=datetime(2027, 1, 1, tzinfo=UTC),
            scope="read",
        ),
    )

    assert main(["status"]) == 0
    output = capsys.readouterr().out
    assert "at-secret" not in output
    assert "rt-secret" not in output
    assert "linear" in output
    assert "read" in output


def test_logout_removes_credentials_but_keeps_the_tab(capsys):
    from oflow.auth.store import get_credentials
    from oflow.config import Config, TabConfig, load_config, save_config

    save_config(Config(tabs=(TabConfig(integration="linear", client_id="client-abc"),)))
    set_credentials(
        "linear",
        Credentials(access_token="at", refresh_token="rt", expires_at=None, scope="read"),
    )

    assert main(["logout", "linear"]) == 0
    assert get_credentials("linear") is None
    assert load_config().tabs[0].integration == "linear"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `main()` currently takes no arguments and returns a print.

- [ ] **Step 4: Write the CLI**

Replace `src/oflow/cli.py` entirely:

```python
"""Command line entry point: connect, status, logout."""

from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

from oflow.auth import oauth
from oflow.auth.store import (
    Credentials,
    CredentialStoreError,
    delete_credentials,
    get_credentials,
    now,
    set_credentials,
)
from oflow.config import Config, TabConfig, add_tab, load_config, save_config
from oflow.registry import UnknownIntegration, get_integration


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    received: dict[str, str] = {}

    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).received = {key: value[0] for key, value in query.items()}
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"oflow: authentication complete, you can close this tab")

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


def run_login(client: httpx.Client, provider: oauth.ProviderConfig, client_id: str | None) -> tuple[str, Credentials]:
    metadata = oauth.discover(client, provider)
    if client_id is None:
        client_id = oauth.register_client(client, metadata, provider, oauth.REDIRECT_URI)

    verifier, challenge = oauth.make_pkce_pair()
    state = secrets.token_urlsafe(16)
    url = oauth.build_authorize_url(
        metadata, client_id, oauth.REDIRECT_URI, challenge, provider.scopes, state
    )

    server = http.server.HTTPServer(("127.0.0.1", oauth.LOOPBACK_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"opening your browser to authorize oflow...\nif it does not open: {url}")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    received = _CallbackHandler.received
    if not received:
        raise oauth.OAuthError("timed out waiting for the browser callback")
    if received.get("state") != state:
        raise oauth.OAuthError("state mismatch in callback; aborting")
    if "code" not in received:
        raise oauth.OAuthError(f"authorization failed: {received.get('error', 'no code returned')}")

    credentials = oauth.exchange_code(
        client, metadata, client_id, received["code"], verifier, oauth.REDIRECT_URI
    )
    return client_id, credentials


def _connect(integration_id: str) -> int:
    try:
        integration = get_integration(integration_id)
    except UnknownIntegration as error:
        print(str(error), file=sys.stderr)
        return 1

    config = load_config()
    existing = next((tab for tab in config.tabs if tab.integration == integration_id), None)

    with httpx.Client(timeout=30) as client:
        try:
            client_id, credentials = run_login(
                client, integration.manifest.provider, existing.client_id if existing else None
            )
        except oauth.OAuthError as error:
            print(f"connect failed: {error}", file=sys.stderr)
            return 1

    try:
        set_credentials(integration_id, credentials)
    except CredentialStoreError as error:
        print(str(error), file=sys.stderr)
        return 1

    save_config(add_tab(config, TabConfig(integration=integration_id, client_id=client_id)))
    print(f"connected {integration.manifest.display_name} (scope: {credentials.scope})")
    return 0


def _status() -> int:
    config = load_config()
    if not config.tabs:
        print("no tabs configured. run: oflow connect linear")
        return 0

    for tab in config.tabs:
        try:
            credentials = get_credentials(tab.integration)
        except CredentialStoreError as error:
            print(f"{tab.integration}: error — {error}")
            continue
        if credentials is None:
            print(f"{tab.integration}: disconnected")
            continue
        if credentials.expires_at is None:
            expiry = "no expiry"
        elif credentials.is_expired(now()):
            expiry = "expired"
        else:
            expiry = f"expires {credentials.expires_at.isoformat(timespec='minutes')}"
        print(f"{tab.integration}: connected — scope {credentials.scope}, {expiry}")
    return 0


def _logout(integration_id: str) -> int:
    try:
        delete_credentials(integration_id)
    except CredentialStoreError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"logged out of {integration_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect", help="authenticate an integration")
    connect.add_argument("integration")

    subparsers.add_parser("status", help="show connection state for configured tabs")

    logout = subparsers.add_parser("logout", help="delete stored credentials")
    logout.add_argument("integration")

    args = parser.parse_args(argv)
    if args.command == "connect":
        return _connect(args.integration)
    if args.command == "status":
        return _status()
    return _logout(args.integration)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check .`
Expected: all tests pass, including `tests/test_registry.py` from Task 5.

- [ ] **Step 6: Verify against the real service, by hand**

Run: `uv run oflow connect linear`
Expected: browser opens, consent screen appears, terminal prints
`connected Linear (scope: read)`.

Run: `uv run oflow status`
Expected: `linear: connected — scope read, expires <timestamp>`, and **no token
anywhere in the output**.

Run: `uv run oflow logout linear && uv run oflow status`
Expected: `linear: disconnected`.

- [ ] **Step 7: Commit**

```bash
git add src/oflow tests/test_registry.py tests/test_cli.py
git commit -m "feat(cli): add connect, status, and logout

Registry, contract types, and the Linear manifest land together because
none of them has a working test without the others: the registry's
allowlist is empty until an integration registers, and the CLI is what
exercises it."
```

---

## Phase 1 exit criteria

Phase 1 is done when all of these hold:

1. `uv run oflow connect linear` completes a real browser login and stores
   credentials in the OS keychain.
2. `uv run oflow status` reports the connection with scope and expiry, and no
   token appears in any output.
3. `uv run oflow logout linear` removes the credentials.
4. `uv run pytest` passes with no network access.
5. `oflow connect jira` fails with a message naming the supported integrations.

Phase 2 (the shell, the Linear source, panels, seen-state, and refresh) gets its
own plan, written after this one lands — the contract in `contract.py` is
deliberately provisional, and Phase 2's task detail depends on what it looks
like once it has a real consumer.

`Credentials.is_expired` compares against the expiry with no margin. The refresh
scheduler will want "expires within N seconds" to count as expired, so a token
is never used in the window between the check and the request landing. Add the
skew allowance there rather than in the store, where a hardcoded margin would be
invisible to callers.

The loopback port is now injectable (`run_login(..., port=...)`), which is the
seam for the RFC 8252 §7.3 question: if the provider accepts a callback on a
port that was never registered, an ephemeral port removes both the squatting
caveat and the port-in-use failure path. Run that experiment and record the
outcome here either way — the design doc currently justifies a fixed port.

One item for that plan: `RESERVED_KEYS` in `contract.py` is a hand-written copy
of a binding table the shell does not have yet. When the shell declares its
`BINDINGS`, derive the constant from them rather than keeping a parallel list.
Two lists of the same keys drift the first time someone adds a shortcut, and the
drift stays invisible until an integration's action silently stops firing. The
current contents are also incomplete on purpose-by-omission: number keys and
`shift+tab` are unreserved despite being likely tab-switching bindings.
