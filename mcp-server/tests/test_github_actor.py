import base64
import pytest

from relay import github, github_credentials, git_process


class Accounts:
    def __init__(self, account):
        self.account = account

    async def require_account(self, provider):
        assert provider == "github"
        if self.account is None:
            raise RuntimeError("sin cuenta")
        return self.account


@pytest.mark.asyncio
async def test_env_usa_solo_actor_actual(monkeypatch):
    account = {"access_token": "token-a", "subject": "1", "login": "alex"}
    monkeypatch.setattr(github_credentials.user_accounts, "require_account", Accounts(account).require_account)
    monkeypatch.setenv("GH_TOKEN", "machine")
    monkeypatch.setenv("GITHUB_TOKEN", "machine")
    monkeypatch.setenv("GH_HOST", "enterprise.example")
    env = await github_credentials.git_env(commit=True)
    assert env["GH_TOKEN"] == "token-a" and env["GITHUB_TOKEN"] == "token-a"
    assert "GH_HOST" not in env and env["GIT_AUTHOR_NAME"] == "alex"
    expected = base64.b64encode(b"x-access-token:token-a").decode()
    assert env["GIT_CONFIG_VALUE_2"] == f"AUTHORIZATION: basic {expected}"
    assert env["GIT_CONFIG_VALUE_0"] == "git@github.com:"
    assert env["GIT_CONFIG_VALUE_1"] == "ssh://git@github.com/"
    assert env["GIT_CONFIG_VALUE_3"] == ""
    assert env["GIT_SSH_COMMAND"]
    assert env["GIT_AUTHOR_EMAIL"] == "1+alex@users.noreply.github.com"

    for key in ("GIT_CONFIG_PARAMETERS", "GIT_TRACE", "GIT_CURL_VERBOSE", "GCM_TRACE"):
        assert key not in env


@pytest.mark.asyncio
async def test_git_options_before_verbo_and_token_never_va_en_argv(monkeypatch):
    account = {"access_token": "token-a", "subject": "1", "login": "alex"}
    monkeypatch.setattr(github_credentials.user_accounts, "require_account", Accounts(account).require_account)
    captured = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def spawn(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return Proc()

    monkeypatch.setattr(git_process.asyncio, "create_subprocess_exec", spawn)
    rc, out = await git_process._git(".", "-c", "credential.helper=x", "fetch", "origin")
    assert rc == 0 and out == "ok"
    assert captured["args"] == ("git", "-c", "credential.helper=x", "fetch", "origin")
    assert "token-a" not in " ".join(captured["args"])
    assert captured["env"]["GIT_CONFIG_VALUE_2"].startswith("AUTHORIZATION: basic ")


@pytest.mark.asyncio
async def test_sin_actor_bloquea_y_no_fallback(monkeypatch):
    monkeypatch.setattr(github_credentials.user_accounts, "require_account", Accounts(None).require_account)
    monkeypatch.setenv("GH_TOKEN", "machine")
    with pytest.raises(RuntimeError):
        await github_credentials.gh_env()


@pytest.mark.asyncio
async def test_gh_json_cachea_por_actor(monkeypatch):
    actors = iter(("subject-a", "subject-b"))
    calls = []
    async def actor_key():
        return next(actors)
    async def fake_gh(*args, **kwargs):
        calls.append(args)
        return 0, '{"actor": %d}' % len(calls)
    monkeypatch.setattr("relay.github_credentials.actor_cache_key", actor_key)
    monkeypatch.setattr(github, "_gh", fake_gh)
    github.clear_cache()
    assert (await github._gh_json(("issues", "org/repo"), "issue", "list"))["actor"] == 1
    assert (await github._gh_json(("issues", "org/repo"), "issue", "list"))["actor"] == 2
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_shell_never_requests_personal_tokens_or_uses_machine(monkeypatch):
    from relay import shell_environment
    monkeypatch.setenv("GH_TOKEN", "machine-token")
    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "machine-config")

    async def account(provider):
        pytest.fail("La shell no debe pedir tokens personales")

    monkeypatch.setattr(github_credentials.user_accounts, "require_account", account)
    connected = shell_environment._env_for_run(base_env=await github_credentials.shell_env())
    assert connected["GH_TOKEN"] == "relay-use-native-github-tools"
    assert connected["GIT_AUTHOR_EMAIL"] == ""
    assert "GH_DEBUG" not in connected and "GIT_CONFIG_PARAMETERS" not in connected

    denied = shell_environment._env_for_run(base_env=await github_credentials.shell_env())
    assert denied["GH_TOKEN"] == "relay-use-native-github-tools"
    assert denied["GIT_CONFIG_VALUE_3"] == ""  # Sin credential helper de la máquina.
    assert denied["GIT_AUTHOR_NAME"] == ""  # Git rechaza commit sin identidad.
    assert denied["GIT_CONFIG_NOSYSTEM"] == "1" and denied["GIT_SSH_COMMAND"]


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_chat_shell_passes_isolated_environment(monkeypatch, background):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from relay import shell_tools
    captured = {}

    async def env():
        return {"GH_TOKEN": "relay-use-native-github-tools"}

    async def run(cmd, **kwargs):
        captured.update(kwargs)
        return {"exit": 0, "out": "ok"}

    @asynccontextmanager
    async def flight(name):
        yield

    monkeypatch.setattr(github_credentials, "shell_env", env)
    monkeypatch.setattr(shell_tools.shell_mod, "run", run)
    monkeypatch.setattr(shell_tools.shell_mod, "lanzar", run)
    tool = shell_tools.shell_tools(repo=".", techo_s=30,
        bitacora=SimpleNamespace(anotar_comando=lambda *args: None), en_vuelo=flight)[0]
    assert await tool.function("echo ok", background=background) == "ok"
    assert captured["env_base"] == {"GH_TOKEN": "relay-use-native-github-tools"}
