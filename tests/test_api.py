import pytest
from fastapi.testclient import TestClient

from veritas import config as C
from veritas.api.app import BridgeUnavailable, RateLimiter, create_app
from veritas.api.models import ArchiveAnnouncement, ClearanceResponse, dossier_code, url_error
from veritas.cogs.archive import BotBridge, archive_embed

from .fakes import FakeBot, FakeGuild, FakeMember, make_channel

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DOMAINS = ("veritas.example.com",)
GOOD = {"number": 42, "title": "Дело о пропавшем свитке", "level": 3,
        "url": "https://veritas.example.com/dossier/42", "reliability": "подтверждено", "author": "Архивариус"}


class FakeBridge:
    def __init__(self, unavailable=False):
        self.posts = []
        self.unavailable = unavailable

    async def clearance(self, discord_id):
        if self.unavailable:
            raise BridgeUnavailable("бот не готов")
        return ClearanceResponse(discord_id=str(discord_id), member=True, level=2, roles=["follower", "liar"],
                                 is_liar=True, is_exiled=False)

    async def announce_archive(self, item):
        self.posts.append(("archive", item))
        return 123

    async def announce_vault(self, item):
        self.posts.append(("vault", item))
        return 456


@pytest.fixture
def bridge():
    return FakeBridge()


@pytest.fixture
def client(bridge):
    return TestClient(create_app(bridge, TOKEN, DOMAINS))


def test_token_required_and_compared(client):
    assert client.get("/api/v1/clearance/1").status_code == 401
    assert client.get("/api/v1/clearance/1", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/clearance/1", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    r = client.get("/api/v1/clearance/1", headers=AUTH)
    assert r.status_code == 200


def test_short_or_empty_token_refused():
    with pytest.raises(ValueError):
        create_app(FakeBridge(), "", DOMAINS)
    with pytest.raises(ValueError):
        create_app(FakeBridge(), "short", DOMAINS)


def test_clearance_shape(client):
    body = client.get("/api/v1/clearance/1558283519849930782", headers=AUTH).json()
    assert body == {"discord_id": "1558283519849930782", "member": True, "level": 2,
                    "roles": ["follower", "liar"], "is_liar": True, "is_exiled": False}


def test_clearance_bad_id(client):
    assert client.get("/api/v1/clearance/abc", headers=AUTH).status_code == 422
    assert client.get("/api/v1/clearance/0", headers=AUTH).status_code == 422


def test_unavailable_bridge_gives_503():
    c = TestClient(create_app(FakeBridge(unavailable=True), TOKEN, DOMAINS))
    assert c.get("/api/v1/clearance/1", headers=AUTH).status_code == 503


def test_archive_announce_ok(client, bridge):
    r = client.post("/api/v1/archive/announce", json=GOOD, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ok": True, "message_id": "123"}
    assert bridge.posts[0][1].number == 42


@pytest.mark.parametrize("patch", [
    {"level": 1}, {"level": 6}, {"number": 0}, {"title": ""}, {"title": "x" * 201},
    {"url": "http://veritas.example.com/x"},           # не https
    {"url": "https://evil.com/x"},                     # чужой домен
    {"url": "https://evilveritas.example.com/x"},      # обход по суффиксу
    {"url": "https://user:pass@veritas.example.com/"},
    {"key": "secret"},                                 # лишние поля запрещены
])
def test_archive_validation(client, bridge, patch):
    body = {**GOOD, **patch}
    r = client.post("/api/v1/archive/announce", json=body, headers=AUTH)
    assert r.status_code == 422, patch
    assert bridge.posts == []


def test_vault_rejects_secrets(client, bridge):
    body = {"title": "Протокол 7", "level": 4, "url": "https://veritas.example.com/v/7", "author": "А",
            "note": "пароль: hunter2"}
    assert client.post("/api/v1/vault/announce", json=body, headers=AUTH).status_code == 422
    body["note"] = "Новый материал по делу"
    r = client.post("/api/v1/vault/announce", json=body, headers=AUTH)
    assert r.status_code == 200 and bridge.posts[-1][0] == "vault"
    body["level"] = 3
    assert client.post("/api/v1/vault/announce", json=body, headers=AUTH).status_code == 422


def test_rate_limit():
    now = [0.0]
    c = TestClient(create_app(FakeBridge(), TOKEN, DOMAINS, rate_limit=3, clock=lambda: now[0]))
    codes = [c.get("/api/v1/clearance/1", headers=AUTH).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    now[0] = 61.0
    assert c.get("/api/v1/clearance/1", headers=AUTH).status_code == 200


def test_rate_limiter_window():
    t = [0.0]
    rl = RateLimiter(2, 10, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a") and rl.allow("b")
    t[0] = 10.0
    assert rl.allow("a")


def test_url_error_without_domains():
    assert "ALLOWED_SITE_DOMAINS" in url_error("https://x.com", [])


def test_no_docs_exposed(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# ───── мост с ботом использует ту же функцию допуска

@pytest.fixture
def bot_env(db):
    guild = FakeGuild()
    bot = FakeBot(db, guild)
    bot.is_ready = lambda: True
    archive = make_channel("text", "📚┃архив")
    guild._channels.append(archive)
    bot.channel_ids["archive"] = archive.id
    return bot, guild, archive


async def test_bridge_member_clearance(bot_env):
    bot, guild, _ = bot_env
    FakeMember(guild, 5, [C.TRUST, C.FOLL, C.LIAR])
    res = await BotBridge(bot).clearance(5)
    assert res.member and res.level == 2 and res.is_liar
    assert res.roles == ["trusted", "follower", "liar"]


async def test_bridge_non_member(bot_env):
    bot, guild, _ = bot_env
    await bot.db.execute("INSERT INTO exiled (user_id, ts) VALUES (?, ?)", (77, 1.0))
    res = await BotBridge(bot).clearance(77)
    assert res.member is False and res.level == 0 and res.is_exiled


async def test_bridge_announce(bot_env):
    bot, guild, archive = bot_env
    mid = await BotBridge(bot).announce_archive(ArchiveAnnouncement(**GOOD))
    assert isinstance(mid, int)
    embed = archive.send.await_args.kwargs["embed"]
    assert embed.title.startswith("📁 ДОСЬЕ-0042")
    assert embed.url == GOOD["url"]


async def test_bridge_unavailable_when_not_ready(bot_env):
    bot, guild, _ = bot_env
    bot.is_ready = lambda: False
    with pytest.raises(BridgeUnavailable):
        await BotBridge(bot).clearance(1)


def test_dossier_code_and_embed():
    assert dossier_code(42) == "ДОСЬЕ-0042"
    e = archive_embed(ArchiveAnnouncement(**GOOD))
    assert any("допуск 3+" in f.value for f in e.fields)


async def test_embedded_server_serves_and_stops():
    import asyncio

    import httpx

    from veritas.api.server import make_server

    server = make_server(create_app(FakeBridge(), TOKEN, DOMAINS), "127.0.0.1", 0)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    async with httpx.AsyncClient(trust_env=False) as http:
        r = await http.get(f"http://127.0.0.1:{port}/api/v1/clearance/5", headers=AUTH)
    assert r.status_code == 200 and r.json()["level"] == 2
    server.should_exit = True
    await asyncio.wait_for(task, 5)
