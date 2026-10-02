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
    # Auch der richtige Token hilft während der Sperre nicht …
    async with client_for(app, ip="192.168.1.77", token=token) as good:
        assert (await good.get("/api/tools")).status_code == 429
    # … aber andere IPs sind nicht betroffen.
    async with client_for(app, ip="192.168.1.78", token=token) as other:
        assert (await other.get("/api/tools")).status_code == 200


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
