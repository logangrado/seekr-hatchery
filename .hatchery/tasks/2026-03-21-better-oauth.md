# Task: better-oauth

**Status**: complete
**Branch**: grado-claude
**Created**: 2026-03-21 09:22

## Objective

Improve Claude OAuth credential handling in the sandbox. When OAuth credentials are detected (`"Claude Code-credentials"` keychain entry), use `Authorization: Bearer` and inject `anthropic-beta: oauth-2025-04-20` instead of the `x-api-key` pattern used for API-key login.

## Context

Claude Code supports two credential types:
- **API key** — `"Claude Code"` keychain, plain string, injected as `x-api-key`
- **OAuth token** — `"Claude Code-credentials"` keychain, JSON with `claudeAiOauth.accessToken`, requires `Authorization: Bearer` + `anthropic-beta: oauth-2025-04-20`

The old `proxy.start_proxy()` API only supported a single `api_key` string and a fixed `inject_header` parameter, making it impossible for backends to inject extra headers (like `anthropic-beta`) or handle credential-type-specific logic.

## Summary

### Design

Replaced the `api_key` + `inject_header` proxy parameters with a `header_mutator` callback. Backends now own all header logic: they strip inbound auth headers and inject credentials in the correct format. The proxy is reduced to hop-by-hop stripping and token validation.

**Key decisions:**
- `make_header_mutator()` raises `RuntimeError` if no credentials are available — callers (`docker.py`) catch and display the error then exit. This eliminates the separate `get_api_key()` + `api_key_missing_hint` pattern.
- Credentials are read at `make_header_mutator()` call time and captured in the closure — simple and sufficient.
- `header_mutator` must be stored as `staticmethod` on the handler class to prevent Python's descriptor protocol from injecting `self` when calling `self.header_mutator(headers)`.

### Files Changed

| File | Change |
|------|--------|
| `src/seekr_hatchery/proxy.py` | Replaced `api_key`/`inject_header` with `header_mutator` callback; `staticmethod` wrapper prevents descriptor binding |
| `src/seekr_hatchery/agents/agent_backend.py` | Removed `get_api_key`/`api_key_missing_hint`; added `make_header_mutator()` abstract method |
| `src/seekr_hatchery/agents/claude.py` | `_get_from_keychain()` now returns `(token, "API_KEY"\|"OAUTH"\|None)`; added `_read_claude_creds()`, `_detect_auth_source()`, `make_header_mutator()` |
| `src/seekr_hatchery/agents/codex.py` | Removed `get_api_key()`; added `make_header_mutator()` with Bearer injection |
| `src/seekr_hatchery/docker.py` | `_run_container()` takes `mutator` instead of `api_key`; `launch_docker*` uses `make_header_mutator()` |
| `tests/conftest.py` | `SpyBackend` updated: removed `get_api_key`/`api_key_missing_hint`, added `make_header_mutator()` |
| `tests/test_proxy.py` | Helpers `_make_api_key_mutator` / `_make_bearer_mutator`; added `TestHeaderMutatorIntegration` |
| `tests/test_agent_claude.py` | Fixed import (`agent` → `agents`); removed `TestGetApiKey`/`TestApiKeyMissingHint`; added `TestDetectAuthSource`, `TestMakeHeaderMutator` |
| `tests/test_agent_codex.py` | Removed `TestGetApiKey`/`TestApiKeyMissingHint`; updated `TestProxyKwargs`; added `TestMakeHeaderMutator` |
| `tests/test_docker.py` | `api_key` → `mutator` parameter |
| `tests/test_pure.py` | `api_key` → `mutator`; added proxy mock in `_run` helpers |

### Gotcha

Python's descriptor protocol converts plain functions set as class attributes into bound methods. `_BoundHandler.header_mutator = staticmethod(header_mutator)` is required so `self.header_mutator(headers)` calls the function with one argument instead of two.
