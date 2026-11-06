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


def new_token(
    hass: HomeAssistant, source: str, duration: float | None, vcodec: str = "",
    *, wallclock: bool = False,
) -> str:
    tokens: dict[str, dict[str, Any]] = hass.data.setdefault(_TOKENS, {})
    now = time.monotonic()
    running = hass.data.get(_HLS, {})
    for k in [k for k, v in tokens.items()
              if v["expires"] < now and not v.get("proc") and k not in running]:
        tokens.pop(k, None)
    token = secrets.token_urlsafe(16)
    tokens[token] = {
        "source": source, "duration": duration, "vcodec": vcodec, "expires": now + TOKEN_TTL_S,
        "wallclock": wallclock,
    }
    return token


def signed_url(hass: HomeAssistant, token: str, refresh_token_id: str | None) -> str:
    from homeassistant.components.http.auth import async_sign_path  # noqa: PLC0415

    return async_sign_path(
        hass, f"{URL_BASE}/{token}", timedelta(seconds=TOKEN_TTL_S),
        refresh_token_id=refresh_token_id,
    )  # fmt: skip


def _retime(entry: dict) -> list[str]:
    """Input options for recordings whose frames all carry one timestamp."""
    return ["-use_wallclock_as_timestamps", "1"] if entry.get("wallclock") else []


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415

        return get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001 - ffmpeg integration not loaded
        return "ffmpeg"


def _drain(stream: asyncio.StreamReader, tail: bytearray) -> asyncio.Task:
    """Keep reading ffmpeg's stderr (a full pipe would stall it); keep the end."""

    async def _run() -> None:
        while chunk := await stream.read(4096):
            tail.extend(chunk)
            del tail[:-2000]

    return asyncio.ensure_future(_run())


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
            *_retime(entry), "-i", entry["source"],
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
        tail = bytearray()
        drain = _drain(proc.stderr, tail)
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
            await proc.wait()
            try:
                await asyncio.wait_for(drain, 2)
            except Exception:  # noqa: BLE001
                drain.cancel()
            err = bytes(tail)
            if entry.get("proc") is proc:
                entry["proc"] = None
        if not started:
            msg = err.decode(errors="replace").strip().replace(entry["source"], "<url>")
            _LOGGER.warning("No video from NVR for the panel: %s", msg[-300:])
            return web.Response(status=502, text=msg[-300:] or "no data")
        return resp


# ── HLS with sound, for Safari / iOS ──────────────────────────────────────
# Safari cannot play an endless MP4 without Range support, and wants HEVC
# tagged hvc1. It plays HLS natively, so ffmpeg writes a small HLS stream
# (video copied, audio AAC) into a temp dir that this view serves.
# Like HA's own /api/hls/<token>/ URLs, the playlist and segments are
# reachable by an unguessable token alone: the native player cannot sign
# each segment request. Tokens are minted only over the authenticated
# websocket.

HLS_BASE = "/api/raysharp_nvr/avhls"
HLS_IDLE_S = 40  # no playlist request for this long -> stop ffmpeg
_HLS = "raysharp_nvr_av_hls"


def hls_url(token: str) -> str:
    return f"{HLS_BASE}/{token}/index.m3u8"


async def _stop_hls(hass: HomeAssistant, token: str) -> None:
    import shutil  # noqa: PLC0415

    run = hass.data.get(_HLS, {}).pop(token, None)
    if not run:
        return
    proc = run["proc"]
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    if run.get("watch"):
        run["watch"].cancel()
    await hass.async_add_executor_job(shutil.rmtree, run["dir"], True)


async def _start_hls(hass: HomeAssistant, token: str, entry: dict) -> dict:
    import tempfile  # noqa: PLC0415

    workdir = await hass.async_add_executor_job(tempfile.mkdtemp, "", "raysharp_hls_")
    duration = entry["duration"]
    tag = ["-tag:v", "hvc1"] if entry.get("vcodec") == "hevc" else []
    cmd = [
        _ffmpeg_binary(hass), "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-timeout", "15000000",
        *_retime(entry), "-i", entry["source"],
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "copy", *tag, "-c:a", "aac", "-b:a", "64k", "-ar", "16000", "-ac", "1",
        "-t", str(int(duration) if duration else MAX_LIVE_S),
        "-f", "hls", "-hls_time", "2", "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", f"{workdir}/s%05d.m4s",
        *(["-hls_playlist_type", "event", "-hls_list_size", "0"] if duration
          else ["-hls_list_size", "6", "-hls_flags", "delete_segments+independent_segments"]),
        f"{workdir}/index.m3u8",
    ]  # fmt: skip
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    tail = bytearray()
    run = {"proc": proc, "dir": workdir, "seen": time.monotonic(), "source": entry["source"],
           "tail": tail, "drain": _drain(proc.stderr, tail)}
    hass.data.setdefault(_HLS, {})[token] = run

    async def _watch() -> None:
        while proc.returncode is None and time.monotonic() - run["seen"] < HLS_IDLE_S:
            await asyncio.sleep(5)
        # A finished clip stays readable until the viewer leaves it.
        while time.monotonic() - run["seen"] < HLS_IDLE_S:
            await asyncio.sleep(5)
        await _stop_hls(hass, token)

    run["watch"] = hass.async_create_background_task(_watch(), f"raysharp_nvr hls {token[:6]}")
    return run


class RaySharpAvHlsView(HomeAssistantView):
    """GET /api/raysharp_nvr/avhls/{token}/{file} -> playlist / init / segments."""

    url = HLS_BASE + "/{token}/{file}"
    name = "api:raysharp_nvr:avhls"
    requires_auth = False
    cors_allowed = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, token: str, file: str) -> web.StreamResponse:
        import os  # noqa: PLC0415
        import re  # noqa: PLC0415

        if not re.fullmatch(r"index\.m3u8|init\.mp4|s\d{5}\.m4s", file):
            return web.Response(status=404)
        run = self.hass.data.get(_HLS, {}).get(token)
        if run is None:
            entry = self.hass.data.get(_TOKENS, {}).get(token)
            if entry is None or file != "index.m3u8" or entry["expires"] < time.monotonic():
                return web.Response(status=404)
            run = await _start_hls(self.hass, token, entry)
        run["seen"] = time.monotonic()
        path = os.path.join(run["dir"], file)
        if file == "index.m3u8":
            # The first playlist appears once ffmpeg has a whole segment.
            for _ in range(60):
                if await self.hass.async_add_executor_job(os.path.isfile, path):
                    break
                if run["proc"].returncode is not None:
                    break
                await asyncio.sleep(0.5)
            else:
                return web.Response(status=504, text="NVR is slow to send video")
            if not await self.hass.async_add_executor_job(os.path.isfile, path):
                await asyncio.sleep(0.5)
                msg = bytes(run["tail"]).decode(errors="replace").strip().replace(run["source"], "<url>")
                _LOGGER.warning("No HLS from NVR for the panel: %s", msg[-300:])
                await _stop_hls(self.hass, token)
                return web.Response(status=502, text=msg[-300:] or "no data")
            body = await self.hass.async_add_executor_job(_read, path)
            return web.Response(body=body, content_type="application/vnd.apple.mpegurl",
                                headers={"Cache-Control": "no-store"})
        if not await self.hass.async_add_executor_job(os.path.isfile, path):
            return web.Response(status=404)
        return web.FileResponse(path, headers={"Content-Type": "video/mp4", "Cache-Control": "no-store"})


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()
