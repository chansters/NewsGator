"""Favicon proxy tests: fetch, cache, failure, validation."""

import httpx
import pytest
from httpx import AsyncClient
from tests.conftest import ADMIN, setup_admin

from app.api import favicons

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    favicons._cache.clear()


async def test_favicon_proxied_and_cached(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    calls = 0

    async def fake_fetch(host: str) -> tuple[bytes, str]:
        nonlocal calls
        calls += 1
        assert host == "news.example.com"
        return PNG, "image/png"

    monkeypatch.setattr(favicons, "_fetch_favicon", fake_fetch)

    r = await client.get("/api/favicon?host=news.example.com")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG
    assert "max-age" in r.headers["cache-control"]

    # Second call served from cache — no new fetch
    r = await client.get("/api/favicon?host=news.example.com")
    assert r.status_code == 200
    assert calls == 1


async def test_favicon_failure_is_404_and_cached(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    calls = 0

    async def fake_fetch(host: str) -> tuple[bytes, str]:
        nonlocal calls
        calls += 1
        raise httpx.HTTPError("boom")

    monkeypatch.setattr(favicons, "_fetch_favicon", fake_fetch)

    assert (await client.get("/api/favicon?host=broken.example.com")).status_code == 404
    assert (await client.get("/api/favicon?host=broken.example.com")).status_code == 404
    assert calls == 1  # failures cached too


async def test_favicon_host_validation(client: AsyncClient) -> None:
    await setup_admin(client)
    assert (await client.get("/api/favicon?host=bad host!")).status_code == 400
    assert (await client.get("/api/favicon?host=..%2Fetc")).status_code == 400


async def test_favicon_parent_domain_fallback(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subdomain with no icon of its own falls back to the parent domain
    (newsletter senders are often mail.example.com)."""
    await setup_admin(client)
    tried: list[str] = []

    async def fake_host_fetch(host: str) -> tuple[bytes, str]:
        tried.append(host)
        if host == "example.com":
            return PNG, "image/png"
        raise ValueError("no favicon found")

    monkeypatch.setattr(favicons, "_fetch_host_favicon", fake_host_fetch)

    r = await client.get("/api/favicon?host=mail.example.com")
    assert r.status_code == 200
    assert r.content == PNG
    assert tried == ["mail.example.com", "example.com"]


async def test_favicon_parent_fallback_stops_at_two_labels(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never strip below the registrable-looking domain (foo.co.uk stays)."""
    await setup_admin(client)
    tried: list[str] = []

    async def fake_host_fetch(host: str) -> tuple[bytes, str]:
        tried.append(host)
        raise ValueError("no favicon found")

    monkeypatch.setattr(favicons, "_fetch_host_favicon", fake_host_fetch)

    assert (await client.get("/api/favicon?host=a.b.co.uk")).status_code == 404
    assert tried == ["a.b.co.uk", "b.co.uk", "co.uk"]


def test_icon_links_parsing() -> None:
    html = '''
      <html><head>
        <link rel="stylesheet" href="/style.css">
        <link rel="shortcut icon" href="/favicon.ico">
        <link href="https://cdn.example.com/icon.png" rel="apple-touch-icon">
        <link rel="icon" type="image/svg+xml" href="/icon.svg">
      </head></html>
    '''
    assert favicons._icon_links(html) == [
        "/favicon.ico",
        "https://cdn.example.com/icon.png",
        "/icon.svg",
    ]
    assert favicons._icon_links("<p>no links</p>") == []


async def test_favicon_requires_auth(client: AsyncClient) -> None:
    await setup_admin(client)
    await client.post("/api/auth/logout")
    client.cookies.clear()
    assert (await client.get("/api/favicon?host=x.com")).status_code == 401
    # ?token= works — <img> tags can't send headers
    r = await client.post("/api/auth/login", json=ADMIN)
    token = r.json()["token"]
    client.cookies.clear()
    assert (
        await client.get(f"/api/favicon?host=bad host!&token={token}")
    ).status_code == 400  # authenticated → validation, not 401
