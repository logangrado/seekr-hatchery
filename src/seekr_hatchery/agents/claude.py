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
from seekr_hatchery.mount import BindMount, Mount, SeedContext, TmpfsMount, VolumeMount

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
    def _get_from_credentials_file() -> tuple[str | None, Literal["API_KEY", "OAUTH"] | None]:
        """Read Claude OAuth token from ~/.claude/.credentials.json (Linux/fallback)."""
        creds_path = Path.home() / ".claude" / ".credentials.json"
        if not creds_path.exists():
            return None, None
        logger.debug("Checking ~/.claude/.credentials.json for OAuth token")
        try:
            data = json.loads(creds_path.read_text())
            access_token = data.get("claudeAiOauth", {}).get("accessToken")
            if access_token:
                logger.debug("Found OAuth token in ~/.claude/.credentials.json")
                return access_token, "OAUTH"
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
        return None, None

    @staticmethod
    def _read_claude_creds() -> tuple[str | None, Literal["API_KEY", "OAUTH"] | None]:
        """Return (credential, source) from env, macOS Keychain, or credentials file."""
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            logger.debug("Using ANTHROPIC_API_KEY from environment")
            return key, "API_KEY"
        cred = ClaudeBackend._get_from_keychain()
        if cred[0] is not None:
            return cred
        logger.debug("Keychain empty, falling back to ~/.claude/.credentials.json")
        return ClaudeBackend._get_from_credentials_file()

    @staticmethod
    def _detect_auth_source() -> Literal["API_KEY", "OAUTH"] | None:
        return ClaudeBackend._read_claude_creds()[1]

    @staticmethod
    def _seed_claude_json(ctx: SeedContext) -> bytes:
        """Seed per-task ~/.claude.json for the VolumeMount (is_file=True).

        Reads host ~/.claude.json (if any), strips auth fields, seeds
        trusted-folder trust, and approves the proxy token.

        Proxy-token approval is belt-and-suspenders: docker mode always
        passes --allow-dangerously-skip-permissions so the prompt is
        suppressed regardless.  On resume the volume is not re-seeded;
        the stale token suffix in customApiKeyResponses is harmless.
        """
        src = Path.home() / ".claude.json"
        try:
            data: dict = json.loads(src.read_text()) if src.exists() else {}
        except (json.JSONDecodeError, OSError):
            data = {}
        for field in _AUTH_FIELDS:
            data.pop(field, None)
        workdir = ctx.container_workdir
        trusted: list[str] = data.get("trustedFolders", [])
        if workdir not in trusted:
            trusted.append(workdir)
            data["trustedFolders"] = trusted
        projects: dict = data.get("projects", {})
        proj: dict = projects.get(workdir, {})
        if not proj.get("hasTrustDialogAccepted"):
            proj["hasTrustDialogAccepted"] = True
            projects[workdir] = proj
            data["projects"] = projects
        data["customApiKeyResponses"] = {"approved": [ctx.proxy_token[-20:]], "rejected": []}
        return json.dumps(data).encode()

    @staticmethod
    def construct_mounts(session_dir: Path) -> list[Mount]:
        mounts: list[Mount] = [
            VolumeMount(name="claude-dir", dst=f"{CONTAINER_HOME}/.claude"),
        ]
        host_claude = Path.home() / ".claude"
        for name in ("projects", "plugins"):
            p = host_claude / name
            if p.exists():
                mounts.append(BindMount(src=p, dst=f"{CONTAINER_HOME}/.claude/{name}", mode="RW"))
        for name in ("settings.json", "mcp-needs-auth-cache.json"):
            p = host_claude / name
            if p.exists():
                mounts.append(BindMount(src=p, dst=f"{CONTAINER_HOME}/.claude/{name}", mode="RW"))
        mounts.append(TmpfsMount(dst=f"{CONTAINER_HOME}/.claude/backups"))
        mounts.append(
            VolumeMount(
                name="claude-json",
                dst=f"{CONTAINER_HOME}/.claude.json",
                is_file=True,
                seed=ClaudeBackend._seed_claude_json,
            )
        )
        fake_creds = session_dir / "fake_credentials.json"
        if fake_creds.exists():
            mounts.append(BindMount(src=fake_creds, dst=f"{CONTAINER_HOME}/.claude/.credentials.json", mode="RW"))
        return mounts

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
        session_dir.mkdir(parents=True, exist_ok=True)

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
        """Write the fake credentials file for the container."""
        (session_dir / "fake_credentials.json").write_text("{}")

    dockerfile_install: str = """\
# ── Claude Code ───────────────────────────────────────────────────────────────
RUN curl -fsSL https://claude.ai/install.sh | bash"""
