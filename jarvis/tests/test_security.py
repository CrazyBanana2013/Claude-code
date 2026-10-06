import pytest

from app.auth import Lockout, load_or_create_token
from app.netguard import is_allowed, parse_networks
from app.config import DEFAULT_NETWORKS
from tests.conftest import client_for

pytestmark = pytest.mark.anyio

PROTECTED = [
    ("GET", "/api/status"),
    ("GET", "/api/tools"),
    ("POST", "/api/tools/led_status"),
    ("POST", "/api/confirm/abc"),
    ("POST", "/api/chat"),
]


async def test_health_without_token(factory):
    app, _ = factory()
    async with client_for(app) as c:
        r = await c.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.parametrize("method,path", PROTECTED)
async def test_protected_routes_need_token(factory, method, path):
    app, _ = factory()
    async with client_for(app) as c:
        r = await c.request(method, path, json={"message": "hi"})
    assert r.status_code == 401


async def test_wrong_token_is_401(factory):
    app, _ = factory()
    async with client_for(app, token="falsch" * 8) as c:
        r = await c.get("/api/tools")
    assert r.status_code == 401


async def test_valid_token_is_200(factory):
    app, token = factory()
    async with client_for(app, token=token) as c:
        r = await c.get("/api/tools")
    assert r.status_code == 200
    assert isinstance(r.json()["tools"], list)


async def test_lockout_after_five_failures(factory):
    app, token = factory()
    async with client_for(app, ip="192.168.1.77", token="x" * 43) as bad:
        for _ in range(5):
            assert (await bad.get("/api/tools")).status_code == 401
        r = await bad.get("/api/tools")
        assert r.status_code == 429
        assert "Retry-After" in r.headers
    # Während der Sperre wird gar nicht verglichen – auch der richtige Token bekommt 429 (kein Weiterraten) …
    async with client_for(app, ip="192.168.1.77", token=token) as good:
        assert (await good.get("/api/tools")).status_code == 429
    # … und andere IPs sind nicht betroffen.
    async with client_for(app, ip="192.168.1.78", token="y" * 43) as other:
        assert (await other.get("/api/tools")).status_code == 401


async def test_lockout_does_not_extend_and_expires(factory):
    """Weitere Versuche während der Sperre verlängern sie nicht; nach 60 s kommt der richtige Token durch."""

    app, token = factory()
    now = [0.0]
    app.state.auth.lockout = Lockout(clock=lambda: now[0])
    async with client_for(app, ip="127.0.0.1", token="x" * 43) as attacker:
        assert [(await attacker.get("/api/tools")).status_code for _ in range(5)] == [401] * 5
        now[0] = 59.0
        for _ in range(20):
            assert (await attacker.get("/api/tools")).status_code == 429
    now[0] = 60.5
    async with client_for(app, ip="127.0.0.1", token=token) as owner:
        assert (await owner.get("/api/tools")).status_code == 200


async def test_requests_without_token_do_not_count_as_failures(factory):
    """Eine fremde Webseite (<img src=http://127.0.0.1:8765/api/…>) schickt keinen Token – das sperrt nichts."""
    app, token = factory()
    async with client_for(app, ip="127.0.0.1") as anon:
        for _ in range(20):
            r = await anon.get("/api/tools")
            assert r.status_code == 401 and r.json()["detail"] == "Token fehlt."
    async with client_for(app, ip="127.0.0.1", token="x" * 43) as wrong:
        assert (await wrong.get("/api/tools")).status_code == 401
    async with client_for(app, ip="127.0.0.1", token=token) as owner:
        assert (await owner.get("/api/tools")).status_code == 200


def test_token_check_needs_no_threadpool_slot():
    """Synchrone Dependencies laufen in FastAPIs Thread-Pool – die Token-Prüfung soll das nicht."""
    import inspect

    from app.auth import TokenAuth

    assert inspect.iscoroutinefunction(TokenAuth.__call__)


async def test_forwarded_for_from_localhost_is_ignored(factory):
    """tailscale serve setzt X-Forwarded-For auf die Tailnet-Adresse des Handys (auch IPv6 fd7a:115c:a1e0::/48).
    Mit den uvicorn-Optionen von `python -m app` zählt nur die echte TCP-Quelle 127.0.0.1."""
    import httpx
    import uvicorn

    from app.__main__ import uvicorn_options

    app, token = factory()
    opts = uvicorn_options(factory.config)
    assert opts["proxy_headers"] is False

    async def get(asgi, path, xff, auth=None):
        headers = {"X-Forwarded-For": xff, **({"Authorization": f"Bearer {auth}"} if auth else {})}
        transport = httpx.ASGITransport(app=asgi, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://jarvis.test") as c:
            return await c.get(path, headers=headers)

    ours = uvicorn.Config(app, **{k: v for k, v in opts.items() if k not in ("host", "port")})
    ours.load()
    assert (await get(ours.loaded_app, "/api/health", "fd7a:115c:a1e0::1234")).status_code == 200
    assert (await get(ours.loaded_app, "/api/tools", "fd7a:115c:a1e0::1234", token)).status_code == 200
    # Gegenprobe: uvicorns Vorgabe (proxy_headers=True) würde die Anfrage mit 403 abweisen.
    default = uvicorn.Config(app)
    default.load()
    assert (await get(default.loaded_app, "/api/health", "fd7a:115c:a1e0::1234")).status_code == 403


def test_lockout_expires_after_60s():
    now = [1000.0]
    lock = Lockout(clock=lambda: now[0])
    for _ in range(5):
        lock.fail("1.2.3.4")
    assert lock.remaining("1.2.3.4") == pytest.approx(60)
    now[0] += 59
    assert lock.remaining("1.2.3.4") > 0
    now[0] += 2
    assert lock.remaining("1.2.3.4") == 0
    lock.fail("1.2.3.4")  # Zähler wurde zurückgesetzt
    assert lock.remaining("1.2.3.4") == 0


def test_success_resets_counter():
    lock = Lockout(clock=lambda: 0.0)
    for _ in range(4):
        lock.fail("ip")
    lock.success("ip")
    lock.fail("ip")
    assert lock.remaining("ip") == 0


@pytest.mark.parametrize("ip", ["8.8.8.8", "203.0.113.5", "100.128.0.1", "172.32.0.1", "2001:db8::1"])
async def test_public_ip_gets_403(factory, ip):
    app, token = factory()
    async with client_for(app, ip=ip, token=token) as c:
        assert (await c.get("/api/health")).status_code == 403
        assert (await c.get("/")).status_code == 403
        assert (await c.get("/api/tools")).status_code == 403


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.1.2.3", "172.16.5.5", "172.31.255.254", "192.168.0.20", "100.64.0.1", "100.127.255.254", "::1", "::ffff:192.168.0.9"]
)
def test_allowed_ranges(ip):
    assert is_allowed(ip, parse_networks(DEFAULT_NETWORKS))


def test_garbage_host_rejected():
    nets = parse_networks(DEFAULT_NETWORKS)
    assert not is_allowed("testclient", nets)
    assert not is_allowed(None, nets)


def test_token_created_once(tmp_path):
    messages = []
    path = tmp_path / "secrets.yaml"
    t1 = load_or_create_token(path, messages.append)
    t2 = load_or_create_token(path, messages.append)
    assert t1 == t2
    assert len(t1) >= 43
    assert len(messages) == 1 and t1 in messages[0]


async def test_every_api_route_except_health_needs_token(factory):
    """Auch künftige Routen (z. B. /api/voice/*): alles unter /api/ außer /api/health verlangt den Token."""
    from fastapi.routing import APIRoute

    def walk(routes):  # FastAPI legt eingebundene Router als eigene Knoten ab (original_router)
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            elif getattr(r, "original_router", None) is not None:
                yield from walk(r.original_router.routes)

    app, _ = factory()
    routes = [(m, r.path) for r in walk(app.routes) if r.path.startswith("/api/") for m in sorted(r.methods)]
    paths = {p for _m, p in routes}
    assert {"/api/voice/status", "/api/voice/speak", "/api/voice/stop", "/api/voice/stt"} <= paths
    async with client_for(app) as c:
        for method, path in routes:
            if path == "/api/health":
                continue
            r = await c.request(method, path.replace("{name}", "led_status").replace("{confirm_id}", "abc"),
                                json={"message": "hi", "text": "hi"})
            assert r.status_code in (401, 429), (method, path, r.status_code)
