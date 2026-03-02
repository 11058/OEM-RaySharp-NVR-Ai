"""WebSocket relay endpoint for RaySharp NVR two-way audio.

URL
---
``GET /api/raysharp_nvr/talk/{entry_id}/{channel}``

Authentication
--------------
Uses HA's standard bearer-token auth.  JavaScript clients must pass the
access token as a query parameter::

    const ws = new WebSocket(
        `/api/raysharp_nvr/talk/${entryId}/${channel}?access_token=${token}`
    );

Wire protocol (binary frames)
------------------------------
* Browser → HA: raw 16-bit LE PCM, 8 kHz, mono, 160 bytes per frame.
* HA → Browser: raw 16-bit LE PCM, 8 kHz, mono.

The SDK handles G.711A encoding/decoding internally (outgoing) or via
``talk_client.decode_g711a`` (incoming).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, DOMAIN
from .talk_client import AUDIO_CHUNK_BYTES, RaySharpTalkClient

_LOGGER = logging.getLogger(__name__)

# URL path for the WebSocket endpoint
TALK_WS_PATH = "/api/raysharp_nvr/talk/{entry_id}/{channel}"
TALK_WS_NAME = "api:raysharp_nvr:talk"


class RaySharpTalkView(HomeAssistantView):
    """Aiohttp WebSocket view that bridges a browser client to the NVR SDK."""

    url = TALK_WS_PATH
    name = TALK_WS_NAME
    requires_auth = True    # HA validates bearer token / access_token query param

    async def get(
        self,
        request: web.Request,
        entry_id: str,
        channel: str,
    ) -> web.WebSocketResponse | web.Response:
        """Handle WebSocket upgrade and run the audio relay loop."""
        hass: HomeAssistant = request.app["hass"]

        # ── Validate config entry ──────────────────────────────────────────────
        config_entry = hass.config_entries.async_get_entry(entry_id)
        if config_entry is None or entry_id not in hass.data.get(DOMAIN, {}):
            return web.Response(status=404, text="Config entry not found")

        try:
            ch = int(channel)
            if not 1 <= ch <= 64:
                raise ValueError
        except ValueError:
            return web.Response(status=400, text="Invalid channel (must be 1–64)")

        # ── Native SDK availability check ──────────────────────────────────────
        lib_base: Path = Path(__file__).parent
        cfg = config_entry.data

        talk = RaySharpTalkClient(
            host=cfg[CONF_HOST],
            port=cfg[CONF_PORT],
            username=cfg[CONF_USERNAME],
            password=cfg[CONF_PASSWORD],
            lib_base=lib_base,
            loop=asyncio.get_event_loop(),
        )

        # ── Initialise SDK (blocking, in executor) ─────────────────────────────
        ok = await hass.async_add_executor_job(talk.initialize)
        if not ok:
            return web.Response(
                status=503,
                text=(
                    "Native SDK library not loaded.  "
                    "Place libSESDKWrapper.so and dependencies in "
                    "custom_components/raysharp_nvr/lib/<arch>/ and restart HA."
                ),
            )

        ok = await hass.async_add_executor_job(talk.connect)
        if not ok:
            return web.Response(status=503, text="SDK login to NVR failed")

        ok = await hass.async_add_executor_job(talk.start_talk, ch)
        if not ok:
            await hass.async_add_executor_job(talk.stop)
            return web.Response(status=503, text="Could not start talk session")

        await hass.async_add_executor_job(talk.start_preview, ch)  # best-effort

        # ── Upgrade to WebSocket ───────────────────────────────────────────────
        ws = web.WebSocketResponse(heartbeat=20.0)
        try:
            await ws.prepare(request)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("WS prepare failed: %s", err)
            await hass.async_add_executor_job(talk.stop)
            return ws

        _LOGGER.info(
            "RaySharp talk WebSocket connected: entry=%s channel=%d", entry_id, ch
        )

        try:
            await _relay_loop(hass, ws, talk)
        finally:
            await hass.async_add_executor_job(talk.stop)
            _LOGGER.info("RaySharp talk WebSocket closed: entry=%s channel=%d", entry_id, ch)

        return ws


async def _relay_loop(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    talk: RaySharpTalkClient,
) -> None:
    """Run send and receive coroutines concurrently until the WS closes."""

    async def _send_to_nvr() -> None:
        """Forward binary PCM frames from browser → NVR (via SDK)."""
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                pcm = msg.data
                # Send in AUDIO_CHUNK_BYTES slices (SDK expects 160-byte chunks)
                for offset in range(0, len(pcm), AUDIO_CHUNK_BYTES):
                    chunk = pcm[offset : offset + AUDIO_CHUNK_BYTES]
                    if len(chunk) == AUDIO_CHUNK_BYTES:
                        await hass.async_add_executor_job(talk.send_pcm, chunk)
            elif msg.type in (
                aiohttp.WSMsgType.ERROR,
                aiohttp.WSMsgType.CLOSE,
            ):
                break

    async def _recv_from_nvr() -> None:
        """Forward decoded PCM frames from NVR audio_queue → browser."""
        while not ws.closed:
            try:
                pcm16 = await asyncio.wait_for(talk.audio_queue.get(), timeout=0.5)
                if not ws.closed:
                    await ws.send_bytes(pcm16)
            except asyncio.TimeoutError:
                continue
            except ConnectionResetError:
                break

    send_task = asyncio.ensure_future(_send_to_nvr())
    recv_task = asyncio.ensure_future(_recv_from_nvr())

    done, pending = await asyncio.wait(
        [send_task, recv_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in done:
        if task.exception():
            _LOGGER.debug("Talk relay task error: %s", task.exception())


def async_register_talk_view(hass: HomeAssistant) -> None:
    """Register the WebSocket view with HA HTTP server.

    Call once during integration setup (idempotent).
    """
    hass.http.register_view(RaySharpTalkView())
    _LOGGER.debug("RaySharp talk WebSocket view registered at %s", TALK_WS_PATH)
