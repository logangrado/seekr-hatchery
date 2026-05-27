#!/usr/bin/env python3

import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from seekr_hatchery import utils
from seekr_hatchery.locks import hatchery_lock

from .agent_backend import CONTAINER_HOME, AgentBackend

logger = logging.getLogger("hatchery")


# Package-bundled skills directory.
_SKILLS_SRC = Path(__file__).parent / "skills"

# Fields in ~/.claude.json that store credentials.  These are stripped from
# per-task copies so the container never sees stored credentials that conflict
# with the proxy token injected as ANTHROPIC_API_KEY.
_AUTH_FIELDS: frozenset[str] = frozenset({"oauthAccount", "apiKey", "primaryApiKey"})


class ClaudeBackend(AgentBackend):
    kind = "CLAUDE"
    binary = "claude"
    supports_sessions = True

    # ── Command construction ───────────────────────────────────────────────────

    @staticmethod
    def build_new_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        args = ["claude"]
        if docker:
            settings = json.dumps({"skipDangerousModePermissionPrompt": True, "trustedFolders": [workdir]})
            args += ["--allow-dangerously-skip-permissions", "--settings", settings]
        args += [
            "--permission-mode=plan",
            f"--append-system-prompt={system_prompt}",
            f"--session-id={session_id}",
            initial_prompt,
        ]
        return args

    @staticmethod
    def build_resume_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str = "",
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        args = ["claude"]
        if docker:
            settings = json.dumps({"skipDangerousModePermissionPrompt": True, "trustedFolders": [workdir]})
            args += ["--allow-dangerously-skip-permissions", "--settings", settings]
        args += [
            "--permission-mode=plan",
            f"--append-system-prompt={system_prompt}",
            f"--resume={session_id}",
        ]
        return args

    @staticmethod
    def build_finalize_command(
        session_id: str,
        system_prompt: str,
        wrap_up_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        args = ["claude"]
        if docker:
            settings = json.dumps({"skipDangerousModePermissionPrompt": True, "trustedFolders": [workdir]})
            args += ["--allow-dangerously-skip-permissions", "--settings", settings]
        args += [
            f"--append-system-prompt={system_prompt}",
            f"--resume={session_id}",
            wrap_up_prompt,
        ]
        return args

    # ── Docker infrastructure ─────────────────────────────────────────────────

    @staticmethod
    def _get_from_keychain() -> tuple[str | None, Literal["API_KEY", "OAUTH"] | None]:
        """Read Claude credentials from the macOS Keychain.

        Checks two entries:
        - ``"Claude Code"``: API-key login → ``(token, "API_KEY")``
        - ``"Claude Code-credentials"``: OAuth login → ``(access_token, "OAUTH")``

        Returns ``(None, None)`` if nothing is found or the platform is not macOS.
        """
        if sys.platform != "darwin":
            return None, None
        logger.debug("Checking macOS Keychain for Claude Code token")

        logger.debug("Checking for API key login")
        result = utils.run(
            ["security", "find-generic-password", "-s", "Claude Code", "-w"],
            check=False,
            sensitive=True,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            if token:
                logger.debug("Found Claude Code token in macOS Keychain")
                return token, "API_KEY"

        logger.debug("Checking for OAuth login")
        result = utils.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            check=False,
            sensitive=True,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
            if raw:
                try:
                    data = json.loads(raw)
                    oauth_data = data.get("claudeAiOauth", {})
                    access_token = oauth_data.get("accessToken")
                    if access_token:
                        return access_token, "OAUTH"
                except (json.JSONDecodeError, AttributeError):
                    pass

        logger.debug("No Claude Code token found in macOS Keychain")
        return None, None

    @staticmethod
    def _read_claude_creds() -> tuple[str | None, Literal["API_KEY", "OAUTH"] | None]:
        """Return (credential, source) from env or macOS Keychain. Single read."""
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            logger.debug("Using ANTHROPIC_API_KEY from environment")
            return key, "API_KEY"
        logger.debug("ANTHROPIC_API_KEY not set, falling back to keychain")
        return ClaudeBackend._get_from_keychain()

    @staticmethod
    def _detect_auth_source() -> Literal["API_KEY", "OAUTH"] | None:
        return ClaudeBackend._read_claude_creds()[1]

    @staticmethod
    def home_mounts(session_dir: Path) -> list[str]:
        mounts = []
        claude_dir = Path.home() / ".claude"
        if claude_dir.exists():
            mounts.append(f"{claude_dir}:{CONTAINER_HOME}/.claude:rw")
        task_json = session_dir / "claude.json"
        if task_json.exists():
            mounts.append(f"{task_json}:{CONTAINER_HOME}/.claude.json:rw")
        return mounts

    @staticmethod
    def tmpfs_paths() -> list[str]:
        # Shadow ~/.claude/backups/ so timestamped credential copies never
        # appear inside the container.
        return [f"{CONTAINER_HOME}/.claude/backups"]

    @staticmethod
    def proxy_kwargs() -> dict:
        return {"target_host": "api.anthropic.com"}

    @staticmethod
    def make_header_mutator() -> Callable[..., dict[str, str]]:
        token, source = ClaudeBackend._read_claude_creds()
        if not token:
            raise RuntimeError("no API token found. Set ANTHROPIC_API_KEY or log in with `claude login` on the host.")

        state: dict = {"token": token}

        def _refresh() -> None:
            """Acquire a cross-process lock, check if already refreshed, then refresh."""
            with hatchery_lock("refresh.claude"):
                # Another process may have already refreshed — check first.
                new_token, _ = ClaudeBackend._read_claude_creds()
                if new_token and new_token != state["token"]:
                    state["token"] = new_token
                    return

                old_token = state["token"]
                proc = subprocess.Popen(
                    ["claude", "-p", "hello"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    new_token, _ = ClaudeBackend._read_claude_creds()
                    if new_token and new_token != old_token:
                        state["token"] = new_token
                        break
                    if proc.poll() is not None:
                        # Process finished — do one final read in case token was
                        # written just before exit.
                        new_token, _ = ClaudeBackend._read_claude_creds()
                        if new_token and new_token != old_token:
                            state["token"] = new_token
                        break
                    time.sleep(0.5)
                else:
                    proc.kill()
                    proc.wait()

        def _mutate(headers: dict[str, str], *, refresh: bool = False) -> dict[str, str]:
            if refresh and source == "OAUTH":
                _refresh()
            out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
            if source == "OAUTH":
                out["Authorization"] = f"Bearer {state['token']}"
                existing = out.get("anthropic-beta", "")
                out["anthropic-beta"] = ("oauth-2025-04-20," + existing) if existing else "oauth-2025-04-20"
            else:
                out["x-api-key"] = state["token"]
            return out

        return _mutate

    @staticmethod
    def container_env(proxy_token: str, proxy_port: int) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": proxy_token,
            "ANTHROPIC_BASE_URL": f"http://host.docker.internal:{proxy_port}",
        }

    @staticmethod
    def on_new_task(session_dir: Path) -> None:
        """Seed a per-task copy of ~/.claude.json with auth fields stripped.

        Copies from the host on first call; subsequent calls (resume) are
        idempotent — the existing copy is kept, but auth fields are stripped
        as a migration guard for pre-sanitisation copies.
        Does nothing if no ~/.claude.json exists on the host.
        """
        src = Path.home() / ".claude.json"
        session_dir.mkdir(parents=True, exist_ok=True)
        task_copy = session_dir / "claude.json"

        if not task_copy.exists():
            if not src.exists():
                return
            try:
                data = json.loads(src.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
            for field in _AUTH_FIELDS:
                data.pop(field, None)
            task_copy.write_text(json.dumps(data))
            logger.debug("Seeded per-task ~/.claude.json (auth stripped) at %s", task_copy)
            return

    @staticmethod
    def _write_skills(worktree: Path) -> None:
        """Copy all skills from the package into the worktree's .claude/skills/ directory."""
        dest_base = worktree / ".claude" / "skills"
        for skill_dir in _SKILLS_SRC.iterdir():
            if not skill_dir.is_dir():
                continue
            dest = dest_base / skill_dir.name
            dest.mkdir(parents=True, exist_ok=True)
            for src_file in skill_dir.iterdir():
                if src_file.is_file():
                    (dest / src_file.name).write_bytes(src_file.read_bytes())
                    logger.debug("Wrote skill file: %s", dest / src_file.name)

    @staticmethod
    def on_before_launch(worktree: Path) -> None:
        """Copy hatchery skills into the worktree's .claude/skills/ directory."""
        ClaudeBackend._write_skills(worktree)

    @staticmethod
    def on_before_container_start(
        session_dir: Path,
        proxy_token: str,
        workdir: str,
    ) -> None:
        """Pre-seed trust and proxy token approval in the per-task claude.json."""
        claude_json = session_dir / "claude.json"
        ClaudeBackend._seed_trusted_folder(claude_json, workdir)
        ClaudeBackend._seed_proxy_token_approval(claude_json, proxy_token)

    @staticmethod
    def _seed_trusted_folder(claude_json: Path, container_workdir: str) -> None:
        """Pre-seed trust for container_workdir in the per-task claude.json.

        Two fields must be set to suppress the "do you trust this folder" prompt:
          - trustedFolders (top-level list)
          - projects.<path>.hasTrustDialogAccepted (per-project flag)

        Claude reads ~/.claude.json before any --settings override is applied, so
        pre-seeding here is the only reliable way to suppress the startup trust
        prompt without PTY injection.
        """
        try:
            data = json.loads(claude_json.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        changed = False
        trusted: list[str] = data.get("trustedFolders", [])
        if container_workdir not in trusted:
            trusted.append(container_workdir)
            data["trustedFolders"] = trusted
            changed = True
        projects: dict = data.get("projects", {})
        project: dict = projects.get(container_workdir, {})
        if not project.get("hasTrustDialogAccepted"):
            project["hasTrustDialogAccepted"] = True
            projects[container_workdir] = project
            data["projects"] = projects
            changed = True
        if changed:
            claude_json.write_text(json.dumps(data))
            logger.debug("Seeded trust for %s in per-task claude.json", container_workdir)

    @staticmethod
    def _seed_proxy_token_approval(claude_json: Path, proxy_token: str) -> None:
        """Pre-approve the proxy token in the per-task claude.json.

        Claude Code stores the last 20 characters of previously-seen custom API
        keys in customApiKeyResponses.approved.  We overwrite the entire approved
        list with only our proxy token suffix — this both suppresses the prompt
        and ensures no real-key suffix from the host copy lingers in the file.
        """
        try:
            data = json.loads(claude_json.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        key_suffix = proxy_token[-20:]
        data["customApiKeyResponses"] = {"approved": [key_suffix], "rejected": []}
        claude_json.write_text(json.dumps(data))
        logger.debug("Set proxy token approval in per-task claude.json")

    dockerfile_install: str = """\
# ── Claude Code ───────────────────────────────────────────────────────────────
RUN curl -fsSL https://claude.ai/install.sh | bash"""
