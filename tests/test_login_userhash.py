"""Digest login against firmware that advertises userhash but ignores it."""

from __future__ import annotations

import asyncio
import hashlib
import re

from aiohttp import web
from conftest import load_module

api = load_module("api_client")
const = load_module("const")

USER, PASSWORD, REALM = "admin", "secret", "device"


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()  # noqa: S324 - Digest needs MD5


def fake_nvr(hashed_works: bool, seen: list):
    async def login(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if not auth:
            return web.Response(
                status=401,
                headers={"WWW-Authenticate":
                         f'Digest realm="{REALM}", nonce="abc", qop="auth", userhash="true"'},
            )  # fmt: skip
        f = dict(re.findall(r'(\w+)="?([^",]*)"?', auth))
        seen.append(f["username"])
        hashed = f.get("userhash") == "true"
        expected_name = md5(f"{USER}:{REALM}") if hashed else USER
        ok_name = f["username"] == expected_name and (hashed_works or not hashed)
        ha1 = md5(f"{USER}:{REALM}:{PASSWORD}")
        ha2 = md5(f"POST:{f['uri']}")
        good = md5(f"{ha1}:{f['nonce']}:{f['nc']}:{f['cnonce']}:{f['qop']}:{ha2}")
        if ok_name and f["response"] == good:
            resp = web.json_response({"result": "success", "data": {"mac_addr": "x"}})
            resp.set_cookie("session", "s1")
            resp.headers["X-csrftoken"] = "t"
            return resp
        return web.json_response(
            {"result": "failed", "reason": "User name or password error",
             "error_code": "login_failed_or_block", "data": {"block_remain_time": "30"}},
            status=400,
        )  # fmt: skip

    app = web.Application()
    app.router.add_post(const.API_LOGIN, login)
    return app


async def _run(hashed_works: bool, password: str = PASSWORD):
    seen: list = []
    runner = web.AppRunner(fake_nvr(hashed_works, seen))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    client = api.RaySharpNVRClient(host="127.0.0.1", port=port, username=USER, password=password)
    try:
        try:
            await client.async_login()
            result = "ok"
        except api.RaySharpNVRAuthError as err:
            result = f"auth: {err}"
        if result == "ok":
            await client.async_login()  # a later re-login
    finally:
        if client._session:  # noqa: SLF001
            await client._session.close()  # noqa: SLF001
        await runner.cleanup()
    return result, seen


def test_advertised_but_broken_userhash_falls_back_and_remembers():
    result, seen = asyncio.run(_run(hashed_works=False))
    assert result == "ok"
    hashed = md5(f"{USER}:{REALM}")
    # hashed refused, plain accepted; the re-login goes straight to plain
    assert seen == [hashed, USER, USER]


def test_working_userhash_is_kept():
    result, seen = asyncio.run(_run(hashed_works=True))
    assert result == "ok" and seen == [md5(f"{USER}:{REALM}")] * 2


def test_wrong_password_is_an_auth_error_with_the_reason():
    result, seen = asyncio.run(_run(hashed_works=False, password="bad"))
    assert result.startswith("auth:") and "blocked for 30 s" in result
    assert len(seen) == 2  # one hashed try, one plain try, no more
