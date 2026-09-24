"""Per-actor GitHub credential and Git identity environment."""
from __future__ import annotations

import base64
import os

from . import user_accounts
from .user_accounts import AccountError


async def _account(provider: str = "github") -> dict:
    return await user_accounts.require_account(provider)


async def actor_cache_key() -> str:
    account = await _account()
    return str(account.get("subject") or account.get("login") or account.get("email"))


async def gh_env(account: dict | None = None) -> dict[str, str]:
    """Return a process environment containing only this actor's GH token."""
    account = account or await _account()
    env = dict(os.environ)
    for key in list(env):
        if (key in {"GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_ENTERPRISE_TOKEN",
                    "GH_DEBUG", "GIT_CONFIG_PARAMETERS", "GIT_CURL_VERBOSE",
                    "GCM_TRACE", "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND"}
                or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TRACE"))):
            env.pop(key, None)
    token = str(account["access_token"])
    env["GH_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    return env


async def git_env(*, commit: bool = False, account: dict | None = None) -> dict[str, str]:
    """Git network/commit environment; tokens never enter argv or URLs."""
    account = account or await _account()
    env = await gh_env(account)
    token = str(account["access_token"])
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    config = {
        0: ("url.https://github.com/.insteadOf", "git@github.com:"),
        1: ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
        2: ("http.https://github.com/.extraheader", f"AUTHORIZATION: basic {basic}"),
        3: ("credential.helper", ""),
    }
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in config.items():
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_SSH_COMMAND"] = "cmd /c exit 1" if os.name == "nt" else "false"
    if commit:
        login = str(account.get("login") or "").strip()
        subject = str(account.get("subject") or "").strip()
        if not login or not subject:
            raise AccountError("La cuenta GitHub no tiene identidad de commit verificable")
        noreply = f"{subject}+{login}@users.noreply.github.com"
        env.update(GIT_AUTHOR_NAME=login, GIT_COMMITTER_NAME=login,
                   GIT_AUTHOR_EMAIL=noreply, GIT_COMMITTER_EMAIL=noreply)
    return env


async def shell_env() -> dict[str, str]:
    """No entregar tokens personales a comandos arbitrarios del modelo."""
    # Git autenticado y commits pasan por git_process; GitHub por tools nativas.
    # ponytail: esto bloquea el fallback habitual, no es un sandbox de SO
    # para la shell arbitraria de Admin o Dev con proyecto asignado.
    env = await git_env(account={"access_token": "relay-use-native-github-tools"})
    env.update(GIT_AUTHOR_NAME="", GIT_COMMITTER_NAME="",
               GIT_AUTHOR_EMAIL="", GIT_COMMITTER_EMAIL="")
    return env
