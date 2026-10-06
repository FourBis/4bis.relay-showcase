"""El cliente conserva el run ante cortes de polling y no aprueba entregas vacias."""
import importlib.util
import json
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler

import pytest


class _Response:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self): return b'{}'


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("RELAY_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_CLIENT_API_KEY", raising=False)
    path = Path(__file__).resolve().parents[2] / "scripts" / "relay_delegate.py"
    spec = importlib.util.spec_from_file_location("relay_delegate_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    return module


def test_req_sends_explicit_credentials_on_get_and_json_post(client, monkeypatch):
    monkeypatch.setenv("RELAY_SESSION_TOKEN", "fake_session_token-123")
    monkeypatch.setenv("RELAY_CLIENT_API_KEY", "fake API key 123")
    requests = []

    def urlopen(req, **_):
        requests.append(req)
        return _Response()

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    client._req("GET", "/status")
    client._req("POST", "/run", {"hello": "world"})
    assert [r.get_header("Cookie") for r in requests] == [
        "relay-session=fake_session_token-123"] * 2
    assert [r.get_header("X-relay-key") for r in requests] == ["fake API key 123"] * 2
    assert json.loads(requests[1].data) == {"hello": "world"}


def test_req_without_credentials_adds_no_auth_headers(client, monkeypatch):
    seen = []

    monkeypatch.setattr(client.urllib.request, "urlopen", lambda req, **_: (seen.append(req) or _Response()))
    client._req("GET", "/status")
    assert seen[0].get_header("Cookie") is None
    assert seen[0].get_header("X-relay-key") is None


@pytest.mark.parametrize("destination_url", ["http://127.0.0.1:8413/next", "https://example.invalid/next"])
def test_auth_headers_are_not_forwarded_by_stdlib_redirect_handler(client, monkeypatch, destination_url):
    monkeypatch.setenv("RELAY_SESSION_TOKEN", "fake_session_token")
    monkeypatch.setenv("RELAY_CLIENT_API_KEY", "fake API key")
    seen = []

    monkeypatch.setattr(client.urllib.request, "urlopen", lambda req, **_: (seen.append(req) or _Response()))
    client._req("POST", "/run", {"hello": "world"})
    source = seen[0]
    destination = HTTPRedirectHandler().redirect_request(
        source, None, 302, "Found", {}, destination_url)
    assert "Cookie" in source.unredirected_hdrs
    assert "X-relay-key" in source.unredirected_hdrs
    assert destination is not None
    assert destination.get_header("Cookie") is None
    assert destination.get_header("X-relay-key") is None
    assert not destination.unredirected_hdrs


@pytest.mark.parametrize("name,value", [
    ("RELAY_SESSION_TOKEN", "fake;session"), ("RELAY_SESSION_TOKEN", "fake\r\nsession"),
    ("RELAY_CLIENT_API_KEY", "fake\r\nkey"),
    ("RELAY_SESSION_TOKEN", "fake\n"), ("RELAY_SESSION_TOKEN", "fake session"),
    ("RELAY_SESSION_TOKEN", "fake-sesión-secreta"), ("RELAY_CLIENT_API_KEY", "fake\tkey"),
    ("RELAY_CLIENT_API_KEY", "fake\x7f"), ("RELAY_CLIENT_API_KEY", "claveá"),
])
def test_req_rejects_invalid_credentials_before_urlopen_without_echoing(client, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    opened = Mock(return_value=_Response())
    monkeypatch.setattr(client.urllib.request, "urlopen", opened)
    with pytest.raises(ValueError) as error:
        client._req("GET", "/status")
    opened.assert_not_called()
    assert value not in str(error.value)


def test_req_allows_printable_api_key_with_internal_spaces(client, monkeypatch):
    monkeypatch.setenv("RELAY_CLIENT_API_KEY", "fake key with spaces")
    req = Mock()

    monkeypatch.setattr(client.urllib.request, "urlopen", lambda request, **_: (req.__setattr__("value", request) or _Response()))
    client._req("GET", "/status")
    assert req.value.get_header("X-relay-key") == "fake key with spaces"


def result(tmp_path, *, answer="Entrega concreta", status="ok", stages=None):
    path = tmp_path / "chat.md"
    path.write_text(f"## Respuesta\n\n{answer}\n", encoding="utf-8")
    return {"status": status, "md_path": str(path), "stages": stages or {}}


def test_transient_poll_timeout_does_not_resubmit(client, monkeypatch, tmp_path):
    request = Mock(side_effect=[{"id": "existing-run"}, TimeoutError(),
                               {"finished": True}, result(tmp_path)])
    monkeypatch.setattr(client, "_req", request)
    assert client.delegate("demo", "tarea", source="codex", author="orchestrator") == 0
    posts = [call for call in request.call_args_list if call.args[0] == "POST"]
    assert len(posts) == 1
    assert posts[0].args[2]["source"] == "codex"
    assert posts[0].args[2]["author"] == "orchestrator"


@pytest.mark.parametrize("status,answer,stages,expected", [
    ("ok", "", {}, 1),
    ("ok", "   ", {}, 1),
    ("ok", "Solo resumen", {"verifier_verdict": "needs_more"}, 1),
    ("ok", "Solo resumen", {"phase_at_end": "no_final_text"}, 1),
    ("running", "Parcial", {}, 2),
    ("cancelled", "Parcial", {}, 1),
    ("ok", "Entrega concreta", {}, 0),
])
def test_resume_requires_finished_nonempty_result(client, monkeypatch, tmp_path,
                                                 status, answer, stages, expected):
    request = Mock(side_effect=[{"finished": True}, result(tmp_path, answer=answer,
                                                           status=status, stages=stages)])
    monkeypatch.setattr(client, "_req", request)
    assert client.delegate(chat_id="existing-run") == expected
    assert all(call.args[0] == "GET" for call in request.call_args_list)


@pytest.mark.parametrize("http_status,polls,expected", [(404, 1, 0), (503, 2, 0), (403, 1, 2)])
def test_status_error_is_not_always_completion(client, monkeypatch, tmp_path,
                                              http_status, polls, expected):
    replies = [HTTPError("local", http_status, "failure", {}, None)]
    if http_status == 503:
        replies.append({"finished": True})
    replies.append(result(tmp_path))
    request = Mock(side_effect=replies)
    monkeypatch.setattr(client, "_req", request)
    assert client.delegate(chat_id="existing-run") == expected
    assert sum("/experts/status/" in call.args[1] for call in request.call_args_list) == polls


def test_deadline_reports_active_run_for_resume(client, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(client, "_req", Mock(return_value=result(tmp_path, status="running")))
    assert client.delegate(chat_id="existing-run", timeout=0) == 2
    assert "--resume existing-run" in capsys.readouterr().err


def test_tool_threshold_cancels_only_the_launched_run(client, monkeypatch, tmp_path):
    request = Mock(side_effect=[{"id": "own-run"}, {"finished": False, "tool_calls": 8},
                               {"cancelled": ["own-run"]}, result(tmp_path, status="cancelled")])
    monkeypatch.setattr(client, "_req", request)
    assert client.delegate("demo", "tarea", max_tools=8) == 1
    posts = [call.args[:2] for call in request.call_args_list if call.args[0] == "POST"]
    assert posts == [("POST", "/experts/run"), ("POST", "/experts/cancel/own-run")]


def test_finished_run_is_not_cancelled_at_threshold(client, monkeypatch, tmp_path):
    request = Mock(side_effect=[{"finished": True, "tool_calls": 8}, result(tmp_path)])
    monkeypatch.setattr(client, "_req", request)
    assert client.delegate(chat_id="done-run", max_tools=8) == 0
    assert all(call.args[0] == "GET" for call in request.call_args_list)


def test_uncertain_cancellation_does_not_resubmit_or_claim_success(client, monkeypatch, capsys):
    request = Mock(side_effect=[{"finished": False, "tool_calls": 9}, TimeoutError()])
    monkeypatch.setattr(client, "_req", request)
    assert client.delegate(chat_id="own-run", max_tools=8) == 2
    error = capsys.readouterr().err
    assert "cancelacion no confirmada" in error
    assert "--resume own-run --max-tools 8" in error
    assert request.call_count == 2


@pytest.mark.parametrize("max_tools", [0, -1])
def test_invalid_tool_threshold_fails_before_request(client, monkeypatch, max_tools):
    request = Mock()
    monkeypatch.setattr(client, "_req", request)
    with pytest.raises(ValueError, match="positivo"):
        client.delegate("demo", "tarea", max_tools=max_tools)
    request.assert_not_called()


def test_cli_rejects_bad_session_without_traceback_or_secret(client):
    env = {**os.environ, "RELAY_SESSION_TOKEN": "fake;private-session"}
    process = subprocess.run([sys.executable, client.__file__, "--list"], env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert process.returncode == 2
    assert "RELAY_SESSION_TOKEN" in process.stderr
    assert "fake;private-session" not in process.stderr
    assert "Traceback" not in process.stderr


def test_launch_rejection_does_not_echo_response_body_or_retry(client, monkeypatch, capsys):
    error = HTTPError("local", 401, "Unauthorized", {}, io.BytesIO(b"reflected-private-token"))
    request = Mock(side_effect=error)
    monkeypatch.setattr(client, "_req", request)
    assert client.delegate("demo", "tarea") == 2
    output = capsys.readouterr()
    assert "401" in output.err
    assert "reflected-private-token" not in output.err + output.out
    assert request.call_count == 1
