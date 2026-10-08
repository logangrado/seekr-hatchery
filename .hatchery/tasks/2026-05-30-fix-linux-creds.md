# Task: fix-linux-creds

**Status**: complete
**Branch**: hatchery/fix-linux-creds
**Created**: 2026-05-30 10:45

## Objective

Fix two bugs affecting Linux Claude users:
1. `_read_claude_creds()` fell back only to macOS Keychain, returning `(None, None)` on Linux — OAuth users hit "no API token found".
2. `~/.claude/.credentials.json` was visible inside the sandbox container (only `backups/` was shadowed).

Also migrate `home_mounts()` + `tmpfs_paths()` → `construct_mounts()` to match upstream/main mount API refactor (#84).

## Context

On Linux, `claude` stores OAuth credentials at `~/.claude/.credentials.json`:
```json
{"claudeAiOauth": {"accessToken": "sk-ant-oat01-..."}}
```

Upstream/main (#84) added `src/seekr_hatchery/mount.py` with a `Mount` dataclass and replaced the separate `home_mounts()` + `tmpfs_paths()` abstract methods with a single `construct_mounts() -> list[Mount]`.

## Summary

**Files changed:**
- `src/seekr_hatchery/agents/claude.py`
- `tests/test_agent_claude.py`

**Merge:** First merged `upstream/main` into the branch to pick up `mount.py` and the `construct_mounts()` abstract method. Resolved one conflict in `agents/__init__.py` (import style difference).

**Credential lookup order** is now:
1. `ANTHROPIC_API_KEY` env var → `"API_KEY"`
2. macOS Keychain → `"API_KEY"` or `"OAUTH"`
3. `~/.claude/.credentials.json` → `"OAUTH"` ← new

**Sandbox protection** follows the same pattern as `CodexBackend`:
- `on_before_container_start()` writes `fake_credentials.json` (`{}`) to `session_dir`
- `construct_mounts()` shadow-mounts it over `{CONTAINER_HOME}/.claude/.credentials.json`
- The backups tmpfs (`Mount(src=None, dst=..., mode="tmpfs")`) prevents timestamped credential copies from appearing in the container

**Test coverage added:**
- `TestGetFromCredentialsFile`: valid token, missing file, malformed JSON, missing nested key
- `TestDetectAuthSource.test_oauth_from_credentials_file`: end-to-end via credentials file
- `TestConstructMounts`: replaces `TestHomeMounts` + `TestTmpfsPaths`; verifies shadow-mount and tmpfs entries
- Updated `test_raises_when_no_credentials` to patch both keychain and credentials file
- Updated `test_seeds_trust_and_token_approval` to assert `fake_credentials.json` is created

**Gotcha:** `test_raises_when_no_credentials` needed to patch `_get_from_credentials_file` in addition to `_get_from_keychain`, otherwise the test's fake home dir causes the credentials file lookup to return `(None, None)` correctly — but on a real dev machine with `~/.claude/.credentials.json` it would fail. Patching both is the right approach.
