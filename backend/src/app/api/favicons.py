"""Source favicon proxy (cached) for story source logos.

Self-hosted: the GUI never hotlinks third-party favicon services. Auth is
required (cookie / Bearer / ?token=) — <img> tags cannot set headers, so the
GUI appends the session token when it has one.
"""

import re
import time
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.api.deps import current_user
from app.core.config import settings
from app.models import User

router = APIRouter(prefix="/favicon", tags=["favicon"])

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", re.IGNORECASE)
_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_REL_RE = re.compile(r'rel=["\']([^"\']+)["\']', re.IGNORECASE)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_MAX_BYTES = 256 * 1024
_MAX_HTML = 512 * 1024
_FAILURE_TTL_S = 3600  # don't re-hit a broken/slow site on every page load

# Bot-protection (Cloudflare & co.) 403s the default python-httpx UA even on
# /favicon.ico (arstechnica, phoronix, …) — present as a plain browser.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/*,*/*;q=0.8",
}

# host -> (expires_at, content or None on failure, media type)
_cache: dict[str, tuple[float, bytes | None, str]] = {}


def _icon_links(html: str) -> list[str]:
    """Hrefs of <link rel="…icon…"> tags, in document order."""
    out: list[str] = []
    for tag in _LINK_RE.findall(html):
        rel = _REL_RE.search(tag)
        href = _HREF_RE.search(tag)
        if rel and href and "icon" in rel.group(1).lower():
            out.append(href.group(1))
    return out


async def _fetch_host_favicon(host: str) -> tuple[bytes, str]:
    """Favicon for one exact host: /favicon.ico first, then the homepage's
    <link rel=icon> (many sites no longer serve the conventional path)."""
    base = f"https://{host}/"
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=5.0, headers=_HEADERS
    ) as client:
        try:
            resp = await client.get(urljoin(base, "favicon.ico"))
            resp.raise_for_status()
            if 0 < len(resp.content) <= _MAX_BYTES:
                media = resp.headers.get("content-type", "image/x-icon").split(";")[0].strip()
                return resp.content, media
        except Exception:
            pass  # fall through to homepage parsing
        resp = await client.get(base)
        resp.raise_for_status()
        for href in _icon_links(resp.text[:_MAX_HTML]):
            url = urljoin(base, href)
            if not url.startswith("https://"):
                continue
            try:
                icon = await client.get(url)
                icon.raise_for_status()
            except Exception:
                continue
            if 0 < len(icon.content) <= _MAX_BYTES:
                media = icon.headers.get("content-type", "image/x-icon").split(";")[0].strip()
                return icon.content, media
        raise ValueError("no favicon found")


async def _fetch_favicon(host: str) -> tuple[bytes, str]:
    """Best-effort favicon: the exact host, then parent domains.

    Newsletter senders are often subdomains (mail.example.com) that serve no
    icon of their own — walk up to the registrable-looking parent (stopping at
    two labels). Module-level seam for tests.
    """
    last_exc: Exception = ValueError("no favicon found")
    candidate = host
    while True:
        try:
            return await _fetch_host_favicon(candidate)
        except Exception as exc:
            last_exc = exc
        labels = candidate.split(".")
        if len(labels) <= 2:
            raise last_exc
        candidate = ".".join(labels[1:])


@router.get("")
async def favicon(
    host: str = Query(min_length=1, max_length=253),
    user: User = Depends(current_user),
) -> Response:
    if not _HOST_RE.match(host) or ".." in host:
        raise HTTPException(400, "Invalid host")
    host = host.lower()
    ttl = settings.favicon_cache_hours * 3600

    cached = _cache.get(host)
    if cached and cached[0] > time.time():
        if cached[1] is None:
            raise HTTPException(404, "No favicon")
        return _icon_response(cached[1], cached[2], ttl)

    try:
        content, media = await _fetch_favicon(host)
    except Exception:
        _cache[host] = (time.time() + _FAILURE_TTL_S, None, "")
        raise HTTPException(404, "No favicon") from None
    _cache[host] = (time.time() + ttl, content, media)
    return _icon_response(content, media, ttl)


def _icon_response(content: bytes, media: str, ttl: int) -> Response:
    return Response(
        content=content,
        media_type=media if media.startswith("image/") else "image/x-icon",
        headers={"Cache-Control": f"public, max-age={min(ttl, 86400)}"},
    )
