"""Video with sound for the panel's camera window.

HA's stream component (HLS/LL-HLS) keeps audio only when it is AAC or MP3.
RaySharp cameras send G.711 (PCMA), so every HLS stream from them is silent.
Here ffmpeg copies the video as is and re-encodes only the audio to AAC,
writing fragmented MP4 that a <video> element plays progressively.

The browser cannot put a bearer token on a <video src>, so the panel gets a
short-lived signed path (HA's own authSig) to an opaque token.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import timedelta
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_TOKENS = "raysharp_nvr_av_tokens"
TOKEN_TTL_S = 300
MAX_LIVE_S = 1800  # a forgotten tab must not hold an NVR session forever
URL_BASE = "/api/raysharp_nvr/av"


def new_token(hass: HomeAssistant, source: str, duration: float | None) -> str:
    tokens: dict[str, dict[str, Any]] = hass.data.setdefault(_TOKENS, {})
    now = time.monotonic()
    for k in [k for k, v in tokens.items() if v["expires"] < now and not v.get("proc")]:
        tokens.pop(k, None)
    token = secrets.token_urlsafe(16)
    tokens[token] = {"source": source, "duration": duration, "expires": now + TOKEN_TTL_S}
    return token


def signed_url(hass: HomeAssistant, token: str, refresh_token_id: str | None) -> str:
    from homeassistant.components.http.auth import async_sign_path  # noqa: PLC0415

    return async_sign_path(
        hass, f"{URL_BASE}/{token}", timedelta(seconds=TOKEN_TTL_S),
        refresh_token_id=refresh_token_id,
    )  # fmt: skip


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415

        return get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001 - ffmpeg integration not loaded
        return "ffmpeg"


class RaySharpAvView(HomeAssistantView):
    """GET /api/raysharp_nvr/av/{token} -> fragmented MP4 with AAC audio."""

    url = URL_BASE + "/{token}"
    name = "api:raysharp_nvr:av"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        tokens: dict = self.hass.data.get(_TOKENS, {})
        entry = tokens.get(token)
        if entry is None or entry["expires"] < time.monotonic() and not entry.get("proc"):
            return web.Response(status=404)
        # One player per token: a reload or a second request replaces the first.
        old = entry.get("proc")
        if old is not None and old.returncode is None:
            old.kill()

        duration = entry["duration"]
        cmd = [
            _ffmpeg_binary(self.hass), "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-timeout", "15000000",
            "-i", entry["source"],
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-ar", "16000", "-ac", "1",
            "-t", str(int(duration) if duration else MAX_LIVE_S),
            "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ]  # fmt: skip
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        entry["proc"] = proc
        resp = web.StreamResponse(
            headers={"Content-Type": "video/mp4", "Cache-Control": "no-store"}
        )
        started = False
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                if not started:
                    await resp.prepare(request)
                    started = True
                await resp.write(chunk)
        except ConnectionResetError:
            pass  # viewer closed the window
        finally:
            if proc.returncode is None:
                proc.kill()
            err = b""
            try:
                err = await asyncio.wait_for(proc.stderr.read(), 2)
            except (TimeoutError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            await proc.wait()
            if entry.get("proc") is proc:
                entry["proc"] = None
        if not started:
            msg = err.decode(errors="replace").strip().replace(entry["source"], "<url>")
            _LOGGER.warning("No video from NVR for the panel: %s", msg[-300:])
            return web.Response(status=502, text=msg[-300:] or "no data")
        return resp
