"""Unit tests for ClaudeBackend."""

import json

import pytest

import seekr_hatchery.agents as agent
from seekr_hatchery.mount import BindMount, Mount, SeedContext, TmpfsMount, VolumeMount

_SETTINGS = json.dumps({"skipDangerousModePermissionPrompt": True, "trustedFolders": ["/w"]})


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestClaudeBackendConstants:
    def test_constants(self):
        assert agent.CLAUDE.kind == "CLAUDE"
        assert agent.CLAUDE.binary == "claude"
        assert agent.CLAUDE.supports_sessions is True


# ---------------------------------------------------------------------------
# build_new_command
# ---------------------------------------------------------------------------


class TestBuildNewCommand:
    def test_native(self):
        cmd = agent.CLAUDE.build_new_command("sid", "sys", "initial")
        assert cmd == [
            "claude",
            "--permission-mode=plan",
            "--append-system-prompt=sys",
            "--session-id=sid",
            "initial",
        ]

    def test_docker(self):
        cmd = agent.CLAUDE.build_new_command("sid", "sys", "initial", docker=True, workdir="/w")
        assert cmd == [
            "claude",
            "--allow-dangerously-skip-permissions",
            "--settings",
            _SETTINGS,
            "--permission-mode=plan",
            "--append-system-prompt=sys",
            "--session-id=sid",
            "initial",
        ]


# ---------------------------------------------------------------------------
# build_resume_command
# ---------------------------------------------------------------------------


class TestBuildResumeCommand:
    def test_native(self):
        # Pass initial_prompt to confirm session-based Claude ignores it
        cmd = agent.CLAUDE.build_resume_command("sid", "sys", "ignored")
        assert cmd == [
            "claude",
            "--permission-mode=plan",
            "--append-system-prompt=sys",
            "--resume=sid",
        ]

    def test_docker(self):
        cmd = agent.CLAUDE.build_resume_command("sid", "sys", docker=True, workdir="/w")
        assert cmd == [
            "claude",
            "--allow-dangerously-skip-permissions",
            "--settings",
            _SETTINGS,
            "--permission-mode=plan",
            "--append-system-prompt=sys",
            "--resume=sid",
        ]


# ---------------------------------------------------------------------------
# build_finalize_command
# ---------------------------------------------------------------------------


class TestBuildFinalizeCommand:
    def test_native(self):
        cmd = agent.CLAUDE.build_finalize_command("sid", "sys", "wrap up")
        assert cmd == [
            "claude",
            "--append-system-prompt=sys",
            "--resume=sid",
            "wrap up",
        ]

    def test_docker(self):
        cmd = agent.CLAUDE.build_finalize_command("sid", "sys", "wrap up", docker=True, workdir="/w")
        assert cmd == [
            "claude",
            "--allow-dangerously-skip-permissions",
            "--settings",
            _SETTINGS,
            "--append-system-prompt=sys",
            "--resume=sid",
            "wrap up",
        ]


# ---------------------------------------------------------------------------
# proxy_kwargs
# ---------------------------------------------------------------------------


class TestProxyKwargs:
    def test_proxy_kwargs(self):
        assert agent.CLAUDE.proxy_kwargs() == {"target_host": "api.anthropic.com"}
        assert "inject_header" not in agent.CLAUDE.proxy_kwargs()


# ---------------------------------------------------------------------------
# _detect_auth_source
# ---------------------------------------------------------------------------


class TestDetectAuthSource:
    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        assert agent.ClaudeBackend._detect_auth_source() == "API_KEY"

    def test_oauth_from_keychain(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(
            agent.ClaudeBackend, "_get_from_keychain", staticmethod(lambda: ("oauth-tok", "OAUTH"))
        )
        assert agent.ClaudeBackend._detect_auth_source() == "OAUTH"

    def test_oauth_from_credentials_file(self, monkeypatch, home):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(
            agent.ClaudeBackend, "_get_from_keychain", staticmethod(lambda: (None, None))
        )
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "file-oauth-tok"}})
        )
        assert agent.ClaudeBackend._detect_auth_source() == "OAUTH"

    def test_none_when_no_creds(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(
            agent.ClaudeBackend, "_get_from_keychain", staticmethod(lambda: (None, None))
        )
        assert agent.ClaudeBackend._detect_auth_source() is None


# ---------------------------------------------------------------------------
# make_header_mutator
# ---------------------------------------------------------------------------


class TestMakeHeaderMutator:
    def test_api_key_mode_injects_xapikey(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        mutator = agent.CLAUDE.make_header_mutator()
        result = mutator({})
        assert result.get("x-api-key") == "test-key-123"
        assert "authorization" not in {k.lower() for k in result}

    def test_oauth_mode_injects_bearer_and_beta(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(
            agent.ClaudeBackend, "_get_from_keychain", staticmethod(lambda: ("oauth-tok", "OAUTH"))
        )
        mutator = agent.CLAUDE.make_header_mutator()
        result = mutator({})
        assert result.get("Authorization") == "Bearer oauth-tok"
        assert result.get("anthropic-beta") == "oauth-2025-04-20"

    def test_oauth_prepends_to_existing_beta(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(
            agent.ClaudeBackend, "_get_from_keychain", staticmethod(lambda: ("oauth-tok", "OAUTH"))
        )
        mutator = agent.CLAUDE.make_header_mutator()
        result = mutator({"anthropic-beta": "existing-beta"})
        assert result.get("anthropic-beta") == "oauth-2025-04-20,existing-beta"

    def test_raises_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(
            agent.ClaudeBackend, "_get_from_keychain", staticmethod(lambda: (None, None))
        )
        monkeypatch.setattr(
            agent.ClaudeBackend, "_get_from_credentials_file", staticmethod(lambda: (None, None))
        )
        with pytest.raises(RuntimeError, match="no API token found"):
            agent.CLAUDE.make_header_mutator()

    def test_strips_inbound_auth_headers(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
        mutator = agent.CLAUDE.make_header_mutator()
        result = mutator({"x-api-key": "proxy-token", "authorization": "Bearer proxy-token", "content-type": "application/json"})
        assert result.get("x-api-key") == "real-key"
        assert "authorization" not in {k.lower() for k in result}
        assert result.get("content-type") == "application/json"


# ---------------------------------------------------------------------------
# container_env
# ---------------------------------------------------------------------------


class TestContainerEnv:
    def test_container_env(self):
        assert agent.CLAUDE.container_env("tok", 9999) == {
            "ANTHROPIC_API_KEY": "tok",
            "ANTHROPIC_BASE_URL": "http://host.docker.internal:9999",
        }


# ---------------------------------------------------------------------------
# on_new_task
# ---------------------------------------------------------------------------


class TestOnNewTask:
    def test_creates_session_dir(self, home, tmp_path):
        session_dir = tmp_path / "session"
        assert not session_dir.exists()
        agent.CLAUDE.on_new_task(session_dir)
        assert session_dir.is_dir()

    def test_idempotent(self, home, tmp_path):
        session_dir = tmp_path / "session"
        agent.CLAUDE.on_new_task(session_dir)
        agent.CLAUDE.on_new_task(session_dir)
        assert session_dir.is_dir()


# ---------------------------------------------------------------------------
# on_before_launch
# ---------------------------------------------------------------------------


class TestOnBeforeLaunch:
    def test_writes_skill_files_to_worktree(self, tmp_path):
        agent.CLAUDE.on_before_launch(tmp_path)
        skills_dir = tmp_path / ".claude" / "skills"
        assert skills_dir.exists()
        skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
        assert skill_dirs, "expected at least one skill directory"
        assert (skills_dir / "hatchery-done" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# on_before_container_start
# ---------------------------------------------------------------------------


class TestOnBeforeContainerStart:
    def test_writes_fake_credentials(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        agent.CLAUDE.on_before_container_start(session_dir, "proxy-token", "/workdir")
        assert (session_dir / "fake_credentials.json").read_text() == "{}"


# ---------------------------------------------------------------------------
# _seed_claude_json
# ---------------------------------------------------------------------------


class TestSeedClaudeJson:
    def _ctx(self, session_dir, proxy_token="tok-1234567890123456789012", workdir="/workdir"):
        return SeedContext(session_dir=session_dir, proxy_token=proxy_token, container_workdir=workdir)

    def test_strips_auth_fields(self, home, tmp_path):
        (home / ".claude.json").write_text(
            json.dumps({"theme": "dark", "oauthAccount": {"secret": "real"}, "apiKey": "sk-real"})
        )
        ctx = self._ctx(tmp_path)
        result = json.loads(agent.ClaudeBackend._seed_claude_json(ctx))
        assert result["theme"] == "dark"
        assert "oauthAccount" not in result
        assert "apiKey" not in result

    def test_seeds_trusted_folders(self, home, tmp_path):
        ctx = self._ctx(tmp_path)
        result = json.loads(agent.ClaudeBackend._seed_claude_json(ctx))
        assert "/workdir" in result["trustedFolders"]
        assert result["projects"]["/workdir"]["hasTrustDialogAccepted"] is True

    def test_approves_proxy_token(self, home, tmp_path):
        token = "abcdefghijklmnopqrstuvwxyz123456"
        ctx = self._ctx(tmp_path, proxy_token=token)
        result = json.loads(agent.ClaudeBackend._seed_claude_json(ctx))
        assert token[-20:] in result["customApiKeyResponses"]["approved"]
        assert result["customApiKeyResponses"]["rejected"] == []

    def test_no_host_file(self, home, tmp_path):
        ctx = self._ctx(tmp_path)
        result = json.loads(agent.ClaudeBackend._seed_claude_json(ctx))
        assert isinstance(result, dict)
        assert "/workdir" in result["trustedFolders"]

    def test_returns_bytes(self, home, tmp_path):
        ctx = self._ctx(tmp_path)
        out = agent.ClaudeBackend._seed_claude_json(ctx)
        assert isinstance(out, bytes)


# ---------------------------------------------------------------------------
# _get_from_credentials_file
# ---------------------------------------------------------------------------


class TestGetFromCredentialsFile:
    def test_valid_token(self, home):
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-abc"}})
        )
        token, source = agent.ClaudeBackend._get_from_credentials_file()
        assert token == "sk-ant-oat01-abc"
        assert source == "OAUTH"

    def test_missing_file(self, home):
        token, source = agent.ClaudeBackend._get_from_credentials_file()
        assert token is None
        assert source is None

    def test_malformed_json(self, home):
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text("not-json")
        token, source = agent.ClaudeBackend._get_from_credentials_file()
        assert token is None
        assert source is None

    def test_missing_nested_key(self, home):
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))
        token, source = agent.ClaudeBackend._get_from_credentials_file()
        assert token is None
        assert source is None


# ---------------------------------------------------------------------------
# construct_mounts
# ---------------------------------------------------------------------------


class TestConstructMounts:
    def test_always_includes_claude_dir_volume(self, home, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        mounts = agent.CLAUDE.construct_mounts(session_dir)
        dsts = [m.dst for m in mounts]
        assert f"{agent.CONTAINER_HOME}/.claude" in dsts

    def test_always_includes_claude_json_volume(self, home, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        mounts = agent.CLAUDE.construct_mounts(session_dir)
        dsts = [m.dst for m in mounts]
        assert f"{agent.CONTAINER_HOME}/.claude.json" in dsts

    def test_claude_json_volume_is_file_seeded(self, home, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        mounts = agent.CLAUDE.construct_mounts(session_dir)
        vol = next(m for m in mounts if m.dst == f"{agent.CONTAINER_HOME}/.claude.json")
        assert isinstance(vol, VolumeMount)
        assert vol.is_file is True
        assert vol.seed is not None

    def test_backups_is_tmpfs(self, home, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        mounts = agent.CLAUDE.construct_mounts(session_dir)
        backups = next(m for m in mounts if m.dst == f"{agent.CONTAINER_HOME}/.claude/backups")
        assert isinstance(backups, TmpfsMount)

    def test_shadow_mounts_credentials_json(self, home, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "fake_credentials.json").write_text("{}")
        mounts = agent.CLAUDE.construct_mounts(session_dir)
        creds = next((m for m in mounts if m.dst == f"{agent.CONTAINER_HOME}/.claude/.credentials.json"), None)
        assert creds is not None
        assert isinstance(creds, BindMount)
        assert creds.mode == "RW"
        assert creds.src == session_dir / "fake_credentials.json"

    def test_no_shadow_without_fake_creds_file(self, home, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        mounts = agent.CLAUDE.construct_mounts(session_dir)
        dsts = [m.dst for m in mounts]
        assert f"{agent.CONTAINER_HOME}/.claude/.credentials.json" not in dsts

    def test_cross_task_binds_when_host_paths_exist(self, home, tmp_path):
        host_claude = home / ".claude"
        host_claude.mkdir()
        (host_claude / "projects").mkdir()
        (host_claude / "settings.json").write_text("{}")
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        mounts = agent.CLAUDE.construct_mounts(session_dir)
        dsts = [m.dst for m in mounts]
        assert f"{agent.CONTAINER_HOME}/.claude/projects" in dsts
        assert f"{agent.CONTAINER_HOME}/.claude/settings.json" in dsts

    def test_no_cross_task_binds_when_host_paths_absent(self, home, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        mounts = agent.CLAUDE.construct_mounts(session_dir)
        dsts = [m.dst for m in mounts]
        assert f"{agent.CONTAINER_HOME}/.claude/projects" not in dsts
        assert f"{agent.CONTAINER_HOME}/.claude/settings.json" not in dsts

    def test_all_mounts_are_mount_objects(self, home, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        mounts = agent.CLAUDE.construct_mounts(session_dir)
        for m in mounts:
            assert isinstance(m, (BindMount, VolumeMount, TmpfsMount))


# ---------------------------------------------------------------------------
# dockerfile_install
# ---------------------------------------------------------------------------


class TestDockerfileInstall:
    def test_dockerfile_install(self):
        snippet = agent.CLAUDE.dockerfile_install
        assert "https://claude.ai/install.sh" in snippet


# ---------------------------------------------------------------------------
# CONTAINER_HOME constant (module-level, used by all backends)
# ---------------------------------------------------------------------------


class TestContainerHome:
    def test_container_home(self):
        assert isinstance(agent.CONTAINER_HOME, str)
        assert agent.CONTAINER_HOME.startswith("/")


# ---------------------------------------------------------------------------
# from_kind (module-level registry)
# ---------------------------------------------------------------------------


class TestFromKind:
    def test_from_string_claude(self):
        assert agent.from_kind("claude") is agent.CLAUDE

    def test_from_string_codex(self):
        assert agent.from_kind("codex") is agent.CODEX

    def test_string_is_case_insensitive(self):
        assert agent.from_kind("CLAUDE") is agent.CLAUDE
        assert agent.from_kind("Codex") is agent.CODEX

    def test_unknown_strings_raise_value_error(self):
        for kind in ["gpt-engineer", "opencode"]:
            with pytest.raises(ValueError, match="unknown agent"):
                agent.from_kind(kind)

    def test_round_trip_via_kind(self):
        for backend in [agent.CLAUDE, agent.CODEX]:
            assert agent.from_kind(backend.kind) is backend
