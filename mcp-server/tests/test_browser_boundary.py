"""Una web externa no debe heredar el acceso owner del socket local."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay import identity, server


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"},
    {"Origin": "https://attacker.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Host": "localhost.attacker.example"},
    {"Host": "localhost@attacker.example"},
])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_browser_request_cannot_reach_owner(headers, method):
    reached = []

    async def mutate(request):
        reached.append(await request.json() if request.method == "POST" else "read")
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[server.browser_guard, identity.access_identity])
    app.router.add_route("*", "/mutation", mutate)
    async with TestClient(TestServer(app)) as client:
        response = await client.request(method, "/mutation", data='{"changed":true}',
                                     headers={"Content-Type": "text/plain", **headers})
        assert response.status == 403
    assert reached == []


async def test_local_cli_and_same_origin_ui_work():
    app = web.Application(middlewares=[server.browser_guard, identity.access_identity])
    app.router.add_get("/identity", lambda r: web.json_response({"identity": identity.requester(r)}))
    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        for headers in ({}, {"Origin": origin, "Sec-Fetch-Site": "same-origin"}):
            response = await client.get("/identity", headers=headers)
            assert response.status == 200
            assert (await response.json())["identity"] == identity.OWNER


async def test_external_link_can_open_ui_but_not_call_api():
    app = web.Application(middlewares=[server.browser_guard, identity.access_identity])
    async def content(request):
        return web.Response(text="UI")
    app.router.add_get("/admin/", content)
    app.router.add_get("/admin/api/config", content)
    async with TestClient(TestServer(app)) as client:
        headers = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
        assert (await client.get("/admin/", headers=headers)).status == 200
        assert (await client.get("/admin/api/config", headers=headers)).status == 403
        assert (await client.get("/admin/", headers={**headers, "Host": "attacker.example"})).status == 403


async def test_public_host_requires_verified_access_token(monkeypatch):
    def verify(token):
        if token != "valid-fixture":
            raise ValueError("invalid token")
        return "member@example.test"

    monkeypatch.setattr(identity, "verify", verify)
    app = web.Application(middlewares=[server.browser_guard, identity.access_identity])
    app.router.add_get("/identity", lambda r: web.json_response({"identity": identity.requester(r)}))
    async with TestClient(TestServer(app)) as client:
        for token, status in (("", 403), ("invalid", 403), ("valid-fixture", 200)):
            response = await client.get("/identity", headers={
                "Host": "relay.example.test", "Origin": "https://relay.example.test",
                "Cf-Access-Jwt-Assertion": token,
            })
            assert response.status == status
            if status == 200:
                assert (await response.json())["identity"] == "member@example.test"
