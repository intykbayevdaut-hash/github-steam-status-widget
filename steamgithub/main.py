"""GitHub Steam Status — dynamic SVG widget API.

FastAPI application intended to run as a Vercel Serverless Function.
Endpoint: GET /api/widget?steam_id=<SteamID64 | vanity name>&theme=<dark|dracula|nord|cyberpunk>
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response

try:  # local development convenience; not required on Vercel
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

STEAM_API = "https://api.steampowered.com"
STEAM64_RE = re.compile(r"7656119\d{10}")
VANITY_RE = re.compile(r"[A-Za-z0-9_\-]{2,32}")

CACHE_OK = "public, max-age=300, s-maxage=300"
CACHE_ERROR = "public, max-age=60, s-maxage=60"

FONT = "'Segoe UI', Ubuntu, 'Helvetica Neue', Arial, sans-serif"

# --------------------------------------------------------------------------- #
# Themes
# --------------------------------------------------------------------------- #
THEMES: Dict[str, Dict[str, str]] = {
    "dark": {
        "bg1": "#0d1117", "bg2": "#1c2333", "border": "#30363d",
        "text": "#e6edf3", "muted": "#8b949e", "accent": "#58a6ff",
        "online": "#3fb950", "ingame": "#58a6ff", "away": "#d29922",
        "offline": "#6e7681", "error": "#f85149",
    },
    "dracula": {
        "bg1": "#282a36", "bg2": "#44475a", "border": "#6272a4",
        "text": "#f8f8f2", "muted": "#b0b4cc", "accent": "#bd93f9",
        "online": "#50fa7b", "ingame": "#8be9fd", "away": "#f1fa8c",
        "offline": "#6272a4", "error": "#ff5555",
    },
    "nord": {
        "bg1": "#2e3440", "bg2": "#3b4252", "border": "#4c566a",
        "text": "#eceff4", "muted": "#a9b4c7", "accent": "#88c0d0",
        "online": "#a3be8c", "ingame": "#81a1c1", "away": "#ebcb8b",
        "offline": "#616e88", "error": "#bf616a",
    },
    "cyberpunk": {
        "bg1": "#0b0014", "bg2": "#2a0a3d", "border": "#ff2a6d",
        "text": "#f5f5ff", "muted": "#b28dff", "accent": "#05d9e8",
        "online": "#39ff14", "ingame": "#05d9e8", "away": "#f9f002",
        "offline": "#7a5c99", "error": "#ff2a6d",
    },
}
DEFAULT_THEME = "dark"


def get_theme(name: Optional[str]) -> Dict[str, str]:
    return THEMES.get((name or "").strip().lower(), THEMES[DEFAULT_THEME])


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def truncate(text: Any, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def fmt_int(value: Optional[int]) -> str:
    return "—" if value is None else f"{value:,}"


def fmt_hours(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 100:
        return f"{round(value):,}"
    return f"{value:.1f}"


def time_ago(timestamp: int) -> str:
    diff = max(0, int(time.time()) - int(timestamp))
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    if diff < 86400 * 30:
        return f"{diff // 86400}d ago"
    if diff < 86400 * 365:
        return f"{diff // (86400 * 30)}mo ago"
    return f"{diff // (86400 * 365)}y ago"


# --------------------------------------------------------------------------- #
# Steam data layer
# --------------------------------------------------------------------------- #
class SteamError(Exception):
    """Error that can be shown to the user inside a fallback SVG."""

    def __init__(self, title: str, detail: str = "") -> None:
        super().__init__(title)
        self.title = title
        self.detail = detail


@dataclass
class Player:
    name: str
    status: str  # online | ingame | away | offline
    status_label: str
    game: Optional[str]
    recent_game: Optional[str]
    last_logoff: Optional[int]
    avatar: Optional[str]  # data: URI
    is_private: bool
    level: Optional[int]
    games_owned: Optional[int]
    hours_total: Optional[float]
    hours_2w: Optional[float]


PERSONA_STATES: Dict[int, Tuple[str, str]] = {
    0: ("offline", "Offline"),
    1: ("online", "Online"),
    2: ("away", "Busy"),
    3: ("away", "Away"),
    4: ("away", "Snooze"),
    5: ("online", "Looking to trade"),
    6: ("online", "Looking to play"),
}


async def steam_get(client: httpx.AsyncClient, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        response = await client.get(f"{STEAM_API}/{path}", params=params)
    except httpx.TimeoutException:
        raise SteamError("Steam API timeout", "Steam did not respond in time. Try again later.")
    except httpx.HTTPError:
        raise SteamError("Steam API unavailable", "Could not reach Steam. Try again later.")

    if response.status_code in (401, 403):
        raise SteamError("Steam API access denied", "The server's STEAM_API_KEY is invalid.")
    if response.status_code == 429:
        raise SteamError("Steam API rate limit", "Too many requests. Try again in a few minutes.")
    if response.status_code >= 500:
        raise SteamError("Steam API unavailable", "Steam is having problems. Try again later.")
    if response.status_code != 200:
        raise SteamError("Steam API error", f"Unexpected response (HTTP {response.status_code}).")

    try:
        data = response.json()
    except ValueError:
        raise SteamError("Steam API error", "Steam returned an unreadable response.")
    if not isinstance(data, dict):
        raise SteamError("Steam API error", "Steam returned an unexpected payload.")
    return data


async def resolve_steam_id(client: httpx.AsyncClient, raw: str, key: str) -> str:
    raw = raw.strip()
    if STEAM64_RE.fullmatch(raw):
        return raw
    if VANITY_RE.fullmatch(raw):
        data = await steam_get(
            client,
            "ISteamUser/ResolveVanityURL/v1/",
            {"key": key, "vanityurl": raw},
        )
        resp = data.get("response") or {}
        if resp.get("success") == 1 and resp.get("steamid"):
            return str(resp["steamid"])
        raise SteamError("Profile not found", "No Steam user matches this custom URL name.")
    raise SteamError("Invalid Steam ID", "Use a 17-digit SteamID64 or your custom URL name.")


async def optional(coro: Any) -> Optional[Dict[str, Any]]:
    """Await a non-critical Steam call; return None instead of raising."""
    try:
        return await coro
    except SteamError:
        return None


async def fetch_avatar(client: httpx.AsyncClient, url: Optional[str]) -> Optional[str]:
    """Download the avatar and inline it as a data URI.

    GitHub proxies README images and blocks external resources referenced
    from inside an SVG, so the avatar has to be embedded.
    """
    if not url:
        return None
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code != 200 or not response.content or len(response.content) > 300_000:
        return None
    content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip().lower()
    if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        content_type = "image/jpeg"
    encoded = base64.b64encode(response.content).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


async def load_player(client: httpx.AsyncClient, raw_id: str, key: str) -> Player:
    steam_id = await resolve_steam_id(client, raw_id, key)

    summaries = await steam_get(
        client,
        "ISteamUser/GetPlayerSummaries/v2/",
        {"key": key, "steamids": steam_id},
    )
    players = (summaries.get("response") or {}).get("players") or []
    if not players:
        raise SteamError("Profile not found", "No Steam user matches this Steam ID.")
    info = players[0]

    owned_data, recent_data, level_data, avatar = await asyncio.gather(
        optional(
            steam_get(
                client,
                "IPlayerService/GetOwnedGames/v1/",
                {"key": key, "steamid": steam_id, "include_played_free_games": 1},
            )
        ),
        optional(
            steam_get(
                client,
                "IPlayerService/GetRecentlyPlayedGames/v1/",
                {"key": key, "steamid": steam_id},
            )
        ),
        optional(
            steam_get(
                client,
                "IPlayerService/GetSteamLevel/v1/",
                {"key": key, "steamid": steam_id},
            )
        ),
        fetch_avatar(client, info.get("avatarfull") or info.get("avatarmedium")),
    )

    games_owned: Optional[int] = None
    hours_total: Optional[float] = None
    owned = (owned_data or {}).get("response") or {}
    if "game_count" in owned:
        games_owned = int(owned.get("game_count") or 0)
        minutes = sum(int(g.get("playtime_forever", 0)) for g in owned.get("games") or [])
        hours_total = minutes / 60.0

    hours_2w: Optional[float] = None
    recent_game: Optional[str] = None
    recent = (recent_data or {}).get("response") or {}
    if recent_data is not None and ("total_count" in recent or "games" in recent):
        recent_games = recent.get("games") or []
        hours_2w = sum(int(g.get("playtime_2weeks", 0)) for g in recent_games) / 60.0
        if recent_games:
            recent_game = recent_games[0].get("name")

    level = ((level_data or {}).get("response") or {}).get("player_level")

    persona_state = int(info.get("personastate", 0))
    status, label = PERSONA_STATES.get(persona_state, ("offline", "Offline"))
    game = info.get("gameextrainfo")
    if game:
        status, label = "ingame", "In-Game"

    return Player(
        name=info.get("personaname") or "Unknown",
        status=status,
        status_label=label,
        game=game,
        recent_game=recent_game,
        last_logoff=info.get("lastlogoff"),
        avatar=avatar,
        is_private=int(info.get("communityvisibilitystate", 1)) != 3,
        level=int(level) if level is not None else None,
        games_owned=games_owned,
        hours_total=hours_total,
        hours_2w=hours_2w,
    )


# --------------------------------------------------------------------------- #
# SVG generation
# --------------------------------------------------------------------------- #
CARD_W = 495
CARD_H = 200
FOOTER_TEXT = "Powered by GitHub Steam Status"


def render_card(player: Player, theme_name: str = DEFAULT_THEME) -> str:
    """Return a complete SVG document (as a string) for the given player."""
    t = get_theme(theme_name)
    color = t.get(player.status, t["offline"])
    animated = player.status in ("online", "ingame")
    pulse_seconds = "1.4" if player.status == "ingame" else "2.4"

    name = escape(truncate(player.name, 24))
    status_label = escape(player.status_label)

    if player.status == "ingame" and player.game:
        line2 = f"Playing {truncate(player.game, 38)}"
    elif player.is_private:
        line2 = "Private profile — stats hidden"
    elif player.status == "offline":
        line2 = f"Last online {time_ago(player.last_logoff)}" if player.last_logoff else "Offline"
    elif player.recent_game:
        line2 = f"Recently played: {truncate(player.recent_game, 30)}"
    else:
        line2 = "Available"
    line2 = escape(line2)

    if player.avatar:
        avatar_svg = (
            f'<image x="26" y="26" width="76" height="76" preserveAspectRatio="xMidYMid slice" '
            f'clip-path="url(#av)" href="{player.avatar}" xlink:href="{player.avatar}"/>'
        )
    else:
        initial = escape((player.name.strip() or "?")[0].upper())
        avatar_svg = (
            f'<circle cx="64" cy="64" r="36" fill="{t["border"]}"/>'
            f'<text x="64" y="74" text-anchor="middle" font-size="30" font-weight="700" '
            f'fill="{t["text"]}">{initial}</text>'
        )

    pulse_svg = '<circle class="pulse" cx="92" cy="92" r="8" fill="{c}"/>'.format(c=color) if animated else ""

    stats = [
        ("Level", fmt_int(player.level)),
        ("Games", fmt_int(player.games_owned)),
        ("Hours", fmt_hours(player.hours_total)),
        ("Last 2 wks", fmt_hours(player.hours_2w)),
    ]
    stats_svg = ""
    for i, (label, value) in enumerate(stats):
        x = 28 + i * 110
        stats_svg += (
            f'<text x="{x}" y="152" class="val">{escape(value)}</text>'
            f'<text x="{x}" y="169" class="lbl">{escape(label.upper())}</text>'
        )

    aria = escape(f"Steam status of {player.name}: {player.status_label}", quote=True)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{CARD_W}" height="{CARD_H}" viewBox="0 0 {CARD_W} {CARD_H}" role="img" aria-label="{aria}">
<title>{aria}</title>
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{t['bg1']}"/>
<stop offset="1" stop-color="{t['bg2']}"/>
</linearGradient>
<linearGradient id="glow" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{t['accent']}" stop-opacity="0.35"/>
<stop offset="1" stop-color="{t['accent']}" stop-opacity="0"/>
</linearGradient>
<clipPath id="card"><rect width="{CARD_W}" height="{CARD_H}" rx="16"/></clipPath>
<clipPath id="av"><circle cx="64" cy="64" r="36"/></clipPath>
</defs>
<style>
text {{ font-family: {FONT}; }}
.name {{ font-size: 22px; font-weight: 700; fill: {t['text']}; }}
.status {{ font-size: 14px; font-weight: 600; fill: {color}; }}
.sub {{ font-size: 12.5px; fill: {t['muted']}; }}
.val {{ font-size: 20px; font-weight: 700; fill: {t['text']}; }}
.lbl {{ font-size: 10px; font-weight: 600; letter-spacing: 0.8px; fill: {t['muted']}; }}
.foot {{ font-size: 9px; fill: {t['muted']}; opacity: 0.75; }}
.pulse {{ transform-box: fill-box; transform-origin: center; animation: pulse {pulse_seconds}s ease-out infinite; }}
.float {{ animation: float 7s ease-in-out infinite; }}
.fade {{ opacity: 0; animation: fadein 0.6s ease-out forwards; }}
@keyframes pulse {{ 0% {{ transform: scale(1); opacity: 0.75; }} 100% {{ transform: scale(2.8); opacity: 0; }} }}
@keyframes float {{ 0%, 100% {{ transform: translateY(0); }} 50% {{ transform: translateY(8px); }} }}
@keyframes fadein {{ from {{ opacity: 0; transform: translateY(5px); }} to {{ opacity: 1; transform: translateY(0); }} }}
</style>
<g clip-path="url(#card)">
<rect width="{CARD_W}" height="{CARD_H}" fill="url(#bg)"/>
<circle class="float" cx="448" cy="22" r="95" fill="url(#glow)"/>
<circle cx="64" cy="64" r="40" fill="none" stroke="{t['accent']}" stroke-opacity="0.55" stroke-width="2"/>
{avatar_svg}
{pulse_svg}
<circle cx="92" cy="92" r="8" fill="{color}" stroke="{t['bg1']}" stroke-width="3"/>
<text class="name fade" x="120" y="54" style="animation-delay:0.05s">{name}</text>
<text class="status fade" x="120" y="77" style="animation-delay:0.15s">{status_label}</text>
<text class="sub fade" x="120" y="98" style="animation-delay:0.25s">{line2}</text>
<line x1="28" y1="122" x2="467" y2="122" stroke="{t['border']}" stroke-opacity="0.7"/>
<g class="fade" style="animation-delay:0.35s">{stats_svg}</g>
<text class="foot" x="{CARD_W / 2}" y="190" text-anchor="middle">{FOOTER_TEXT}</text>
</g>
<rect x="0.5" y="0.5" width="{CARD_W - 1}" height="{CARD_H - 1}" rx="15.5" fill="none" stroke="{t['border']}"/>
</svg>"""


def render_error(title: str, detail: str = "", theme_name: str = DEFAULT_THEME) -> str:
    """Return a compact fallback SVG describing an error."""
    t = get_theme(theme_name)
    height = 110
    title_e = escape(truncate(title, 40))
    detail_e = escape(truncate(detail, 62))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_W}" height="{height}" viewBox="0 0 {CARD_W} {height}" role="img" aria-label="{escape(title, quote=True)}">
<title>{escape(title)}</title>
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{t['bg1']}"/>
<stop offset="1" stop-color="{t['bg2']}"/>
</linearGradient>
<clipPath id="card"><rect width="{CARD_W}" height="{height}" rx="16"/></clipPath>
</defs>
<style>
text {{ font-family: {FONT}; }}
.t {{ font-size: 18px; font-weight: 700; fill: {t['text']}; }}
.d {{ font-size: 12.5px; fill: {t['muted']}; }}
.foot {{ font-size: 9px; fill: {t['muted']}; opacity: 0.75; }}
.ring {{ transform-box: fill-box; transform-origin: center; animation: pulse 2s ease-out infinite; }}
@keyframes pulse {{ 0% {{ transform: scale(1); opacity: 0.7; }} 100% {{ transform: scale(1.9); opacity: 0; }} }}
</style>
<g clip-path="url(#card)">
<rect width="{CARD_W}" height="{height}" fill="url(#bg)"/>
<circle class="ring" cx="54" cy="46" r="20" fill="{t['error']}"/>
<circle cx="54" cy="46" r="20" fill="{t['error']}"/>
<text x="54" y="54" text-anchor="middle" font-size="24" font-weight="700" fill="{t['bg1']}">!</text>
<text class="t" x="92" y="42">{title_e}</text>
<text class="d" x="92" y="63">{detail_e}</text>
<text class="foot" x="{CARD_W / 2}" y="98" text-anchor="middle">{FOOTER_TEXT}</text>
</g>
<rect x="0.5" y="0.5" width="{CARD_W - 1}" height="{height - 1}" rx="15.5" fill="none" stroke="{t['border']}"/>
</svg>"""


# --------------------------------------------------------------------------- #
# FastAPI application
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="GitHub Steam Status",
    description="Dynamic SVG Steam status widget for GitHub README files.",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def svg_response(svg: str, ok: bool = True) -> Response:
    return Response(
        content=svg,
        media_type="image/svg+xml; charset=utf-8",
        headers={
            "Cache-Control": CACHE_OK if ok else CACHE_ERROR,
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/widget")
async def widget(
    steam_id: Optional[str] = Query(default=None, max_length=64),
    theme: str = Query(default=DEFAULT_THEME, max_length=32),
) -> Response:
    """Render the Steam status card as an SVG image.

    Errors are also returned as SVG (HTTP 200) so that they are visible
    inside a README instead of a broken-image icon.
    """
    if not steam_id or not steam_id.strip():
        return svg_response(
            render_error("Missing steam_id", "Use /api/widget?steam_id=YOUR_STEAM_ID", theme),
            ok=False,
        )

    key = os.getenv("STEAM_API_KEY", "").strip()
    if not key:
        return svg_response(
            render_error("Server not configured", "STEAM_API_KEY is not set on the server.", theme),
            ok=False,
        )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            follow_redirects=True,
            headers={"User-Agent": "github-steam-status/1.0"},
        ) as client:
            player = await load_player(client, steam_id, key)
        return svg_response(render_card(player, theme), ok=True)
    except SteamError as exc:
        return svg_response(render_error(exc.title, exc.detail, theme), ok=False)
    except Exception:  # noqa: BLE001 - never leak a stack trace into a README image
        return svg_response(
            render_error("Unexpected error", "Something went wrong. Try again later.", theme),
            ok=False,
        )


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/")
async def index() -> JSONResponse:
    return JSONResponse(
        {
            "name": "GitHub Steam Status",
            "usage": "/api/widget?steam_id=YOUR_STEAM_ID&theme=dark",
            "themes": sorted(THEMES.keys()),
        }
    )
