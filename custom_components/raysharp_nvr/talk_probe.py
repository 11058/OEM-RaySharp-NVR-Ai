"""Does the camera's RTSP source offer an audio back-channel (ONVIF Profile T)?

Home Assistant 2026.11 talks to cameras through go2rtc: the browser's
microphone goes over WebRTC to go2rtc, which sends it to the camera on the
RTSP back-channel - a `sendonly` audio track the server lists only when the
DESCRIBE carries `Require: www.onvif.org/ver20/backchannel`. This module asks
exactly that and reports what came back. Read-only: DESCRIBE, no SETUP/PLAY.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any
from urllib.parse import unquote, urlparse

REQUIRE = "www.onvif.org/ver20/backchannel"


def _digest(user: str, pw: str, method: str, uri: str, hdr: str) -> str:
    f = dict(re.findall(r'(\w+)="?([^",]*)"?', hdr))
    realm, nonce = f.get("realm", ""), f.get("nonce", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{pw}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp}"')


async def _describe(url: str, auth: str | None, timeout: float) -> tuple[int, dict, str]:
    u = urlparse(url)
    port = u.port or 554
    clean = url.replace(f"{u.username}:{u.password}@", "") if u.username else url
    reader, writer = await asyncio.wait_for(asyncio.open_connection(u.hostname, port), timeout)
    try:
        req = (f"DESCRIBE {clean} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
               f"Require: {REQUIRE}\r\nUser-Agent: HomeAssistant-raysharp\r\n")
        if auth:
            req += f"Authorization: {auth}\r\n"
        writer.write((req + "\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        lines = head.decode(errors="replace").split("\r\n")
        status = int(lines[0].split()[1]) if len(lines[0].split()) > 1 else 0
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers.setdefault(k.strip().lower(), v.strip())
        body = b""
        length = int(headers.get("content-length", "0") or 0)
        if length:
            body = await asyncio.wait_for(reader.readexactly(length), timeout)
        return status, headers, body.decode(errors="replace")
    finally:
        writer.close()


def parse_sdp(sdp: str) -> list[dict[str, Any]]:
    """media sections: kind, direction, codecs."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    session_dir = "sendrecv"
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            kind = line[2:].split()[0]
            cur = {"kind": kind, "direction": None, "codecs": []}
            out.append(cur)
        elif line in ("a=sendonly", "a=recvonly", "a=sendrecv", "a=inactive"):
            if cur is None:
                session_dir = line[2:]
            else:
                cur["direction"] = line[2:]
        elif line.startswith("a=rtpmap:") and cur is not None:
            cur["codecs"].append(line.split(" ", 1)[1] if " " in line else line)
    for m in out:
        m["direction"] = m["direction"] or session_dir
    return out


async def probe(url: str, timeout: float = 8) -> dict[str, Any]:
    """{ok, status, backchannel, codecs, media, why}. Never raises."""
    u = urlparse(url)
    user, pw = unquote(u.username or ""), unquote(u.password or "")
    try:
        status, headers, sdp = await _describe(url, None, timeout)
        if status == 401 and user:
            www = headers.get("www-authenticate", "")
            clean = url.replace(f"{u.username}:{u.password}@", "")
            if www.lower().startswith("digest"):
                auth = _digest(user, pw, "DESCRIBE", clean, www)
            else:
                import base64  # noqa: PLC0415

                auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
            status, headers, sdp = await _describe(url, auth, timeout)
    except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError) as err:
        return {"ok": False, "status": 0, "backchannel": False, "why": str(err) or type(err).__name__}
    if status == 551:
        return {"ok": True, "status": status, "backchannel": False,
                "why": "регистратор не поддерживает обратный аудиоканал (551 Option not supported)"}
    if status != 200:
        return {"ok": False, "status": status, "backchannel": False, "why": f"RTSP {status}"}
    media = parse_sdp(sdp)
    back = [m for m in media if m["kind"] == "audio" and m["direction"] == "sendonly"]
    return {
        "ok": True, "status": status, "backchannel": bool(back),
        "codecs": back[0]["codecs"] if back else [],
        "media": [{"kind": m["kind"], "direction": m["direction"], "codecs": m["codecs"][:3]} for m in media],
        "why": "" if back else "в ответе нет обратного аудиоканала (нет audio a=sendonly)",
    }
