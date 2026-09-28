# Task: update-cluade

**Status**: complete
**Branch**: claude
**Created**: 2026-06-17 21:11

## Objective

Bring `agents/claude.py` up to date with the v0.42/v0.43 mount architecture changes. Move `~/.claude/` and `~/.claude.json` off virtio-fs using VolumeMount + overlay BindMount, matching the pattern established for Codex.

## Context

After merging upstream v0.42 (tagged-union mount types: `BindMount | VolumeMount | TmpfsMount`) and v0.43 (host-path mirroring), `claude.py` still called the old `Mount(src=..., mode="rw")` constructors which are now broken — `Mount` is a type alias, not a callable.

## Summary

**Changes made to `src/seekr_hatchery/agents/claude.py`:**

- Updated import: `Mount` → `BindMount, Mount, SeedContext, TmpfsMount, VolumeMount`
- Added `_seed_claude_json(ctx: SeedContext) -> bytes`: consolidates the old `on_new_task` file-copy + `on_before_container_start` trust/token seeding into a single seed callable. Reads host `~/.claude.json`, strips auth fields, seeds `trustedFolders` + `hasTrustDialogAccepted`, and approves the proxy token suffix.
- Rewrote `construct_mounts`: per-task `VolumeMount("claude-dir")` for `~/.claude`, cross-task `BindMount`s for `projects/`, `plugins/`, `settings.json`, `mcp-needs-auth-cache.json` (only when they exist on host), `TmpfsMount` for `~/.claude/backups`, `VolumeMount("claude-json", is_file=True, seed=_seed_claude_json)` for `~/.claude.json`, and conditional `BindMount` for `fake_credentials.json`.
- Simplified `on_new_task`: only `session_dir.mkdir(parents=True, exist_ok=True)`.
- Simplified `on_before_container_start`: only writes `fake_credentials.json` (trust + proxy token moved to seed callable).
- Removed `_seed_trusted_folder` and `_seed_proxy_token_approval` — logic inlined in `_seed_claude_json`.

**Tests updated (`tests/test_agent_claude.py`):**
- `TestOnNewTask`: replaced file-copy tests with `mkdir` tests.
- `TestOnBeforeContainerStart`: stripped trust/token tests (logic now in seed), kept `fake_credentials` write test.
- Added `TestSeedClaudeJson`: covers auth stripping, trust seeding, token approval, missing host file, return type.
- `TestConstructMounts`: updated for VolumeMount/TmpfsMount/BindMount types; removed `test_missing_claude_dir` (VolumeMount always emitted now); added cross-task bind tests.

**Key decisions:**
- `~/.claude` is always volume-mounted (per-task isolation), regardless of whether `~/.claude` exists on the host — the volume starts empty on first use, which is fine.
- On resume, seed is skipped (`created=False`). Stale proxy token in `customApiKeyResponses` is harmless because docker mode passes `--allow-dangerously-skip-permissions`.
- Cross-task shared paths (projects, plugins, settings, mcp cache) use conditional BindMounts — only emitted when the host path exists, matching the Codex pattern.
