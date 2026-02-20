"""API client for RaySharp NVR with HTTP Digest authentication."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from typing import Any

import aiohttp

from .const import API_HEARTBEAT, API_LOGIN, API_LOGOUT

_LOGGER = logging.getLogger(__name__)


class RaySharpNVRAuthError(Exception):
    """Exception for authentication errors."""


class RaySharpNVRConnectionError(Exception):
    """Exception for connection errors."""


def _md5(text: str) -> str:
    """Return MD5 hex digest of a string."""
    return hashlib.md5(text.encode()).hexdigest()


def _parse_digest_challenge(header: str) -> dict[str, str]:
    """Parse WWW-Authenticate: Digest header into a dict."""
    params: dict[str, str] = {}
    # Remove "Digest " prefix
    header = header.strip()
    if header.lower().startswith("digest "):
        header = header[7:]

    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([\w]+))', header):
        key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        params[key] = value

    return params


def _build_digest_header(
    username: str,
    password: str,
    method: str,
    uri: str,
    challenge: dict[str, str],
    nc: int,
) -> str:
    """Build Authorization: Digest header value."""
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "")
    use_userhash = challenge.get("userhash", "").lower() == "true"

    cnonce = os.urandom(16).hex()
    nc_str = f"{nc:08x}"

    # HA1 = MD5(username:realm:password)
    ha1 = _md5(f"{username}:{realm}:{password}")

    # HA2 = MD5(method:uri)
    ha2 = _md5(f"{method}:{uri}")

    # Response with qop=auth
    if "auth" in qop:
        response = _md5(f"{ha1}:{nonce}:{nc_str}:{cnonce}:{qop}:{ha2}")
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")

    # Username to send (userhash if required)
    if use_userhash:
        username_value = _md5(f"{username}:{realm}")
    else:
        username_value = username

    parts = [
        f'username="{username_value}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'cnonce="{cnonce}"',
        f"nc={nc_str}",
        f"qop={qop}",
        f'response="{response}"',
    ]
    if use_userhash:
        parts.append("userhash=true")

    return "Digest " + ", ".join(parts)


class RaySharpNVRClient:
    """HTTP client for RaySharp NVR with Digest auth, session, CSRF, and heartbeat."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the client."""
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._base_url = f"http://{host}:{port}"
        self._external_session = session is not None
        self._session = session
        self._csrf_token: str | None = None
        self._session_cookie: str | None = None
        self._authenticated = False
        self._nc = 0
        self._digest_challenge: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    @property
    def authenticated(self) -> bool:
        """Return whether the client is authenticated."""
        return self._authenticated

    def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._external_session = False
        return self._session

    async def async_login(self) -> dict[str, Any]:
        """Authenticate with the NVR using HTTP Digest auth.

        Two-step process:
        1. POST without auth → get 401 + Digest challenge
        2. POST with Digest Authorization header → get 200 + session cookie + CSRF
        """
        session = self._get_session()
        url = f"{self._base_url}{API_LOGIN}"
        payload = {"version": "1.0", "data": {}}
        timeout = aiohttp.ClientTimeout(total=15)

        try:
            # Step 1: Get digest challenge
            async with session.post(
                url,
                json=payload,
                timeout=timeout,
            ) as resp:
                if resp.status != 401:
                    if resp.status == 200:
                        # Already authenticated somehow
                        return await self._handle_login_success(resp)
                    raise RaySharpNVRConnectionError(
                        f"Expected 401 challenge, got {resp.status}"
                    )

                www_auth = resp.headers.get("WWW-Authenticate", "")
                if not www_auth.lower().startswith("digest"):
                    raise RaySharpNVRAuthError(
                        "Server does not support Digest authentication"
                    )

                self._digest_challenge = _parse_digest_challenge(www_auth)

            # Step 2: Respond to challenge
            self._nc += 1
            auth_header = _build_digest_header(
                self._username,
                self._password,
                "POST",
                API_LOGIN,
                self._digest_challenge,
                self._nc,
            )

            async with session.post(
                url,
                json=payload,
                headers={"Authorization": auth_header},
                timeout=timeout,
            ) as resp:
                if resp.status == 401:
                    self._authenticated = False
                    raise RaySharpNVRAuthError(
                        "Authentication failed: invalid credentials"
                    )
                if resp.status != 200:
                    raise RaySharpNVRConnectionError(
                        f"Login failed with status {resp.status}"
                    )

                return await self._handle_login_success(resp)

        except aiohttp.ClientError as err:
            raise RaySharpNVRConnectionError(
                f"Connection to NVR failed: {err}"
            ) from err

    async def _handle_login_success(
        self, resp: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        """Extract session data from successful login response."""
        # Extract CSRF token
        csrf = resp.headers.get("X-csrftoken") or resp.headers.get("X-CsrfToken")
        if csrf:
            self._csrf_token = csrf

        # Extract session cookie
        for cookie in resp.cookies.values():
            if cookie.key.startswith("session"):
                self._session_cookie = f"{cookie.key}={cookie.value}"
                break

        data = await resp.json(content_type=None)
        self._authenticated = True
        _LOGGER.debug("Successfully logged in to NVR at %s", self._host)
        return data

    def _build_headers(self) -> dict[str, str]:
        """Build request headers with CSRF token and session cookie."""
        headers: dict[str, str] = {}
        if self._csrf_token:
            headers["X-csrftoken"] = self._csrf_token
        if self._session_cookie:
            headers["Cookie"] = self._session_cookie
        return headers

    async def async_heartbeat(self) -> bool:
        """Send heartbeat to keep session alive."""
        try:
            await self.async_api_call(API_HEARTBEAT)
            return True
        except (RaySharpNVRAuthError, RaySharpNVRConnectionError):
            _LOGGER.warning("Heartbeat failed, session may have expired")
            return False

    async def async_api_call(
        self, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make an authenticated API call to the NVR.

        Uses session cookie + CSRF token from login.
        Re-authenticates on 401 responses.
        """
        session = self._get_session()
        url = f"{self._base_url}{path}"
        payload = {"version": "1.0", "data": data or {}}
        headers = self._build_headers()

        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    _LOGGER.debug("Got 401 on %s, attempting re-login", path)
                    async with self._lock:
                        await self.async_login()
                    return await self._raw_api_call(path, data)

                if resp.status != 200:
                    raise RaySharpNVRConnectionError(
                        f"API call to {path} failed with status {resp.status}"
                    )

                # Update CSRF token if provided
                csrf = (
                    resp.headers.get("X-csrftoken")
                    or resp.headers.get("X-CsrfToken")
                )
                if csrf:
                    self._csrf_token = csrf

                return await resp.json(content_type=None)

        except aiohttp.ClientError as err:
            raise RaySharpNVRConnectionError(
                f"API call to {path} failed: {err}"
            ) from err

    async def _raw_api_call(
        self, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make an API call without re-login retry (to avoid recursion)."""
        session = self._get_session()
        url = f"{self._base_url}{path}"
        payload = {"version": "1.0", "data": data or {}}
        headers = self._build_headers()

        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    self._authenticated = False
                    raise RaySharpNVRAuthError("Re-authentication failed")

                if resp.status != 200:
                    raise RaySharpNVRConnectionError(
                        f"API call to {path} failed with status {resp.status}"
                    )

                csrf = (
                    resp.headers.get("X-csrftoken")
                    or resp.headers.get("X-CsrfToken")
                )
                if csrf:
                    self._csrf_token = csrf

                return await resp.json(content_type=None)

        except aiohttp.ClientError as err:
            raise RaySharpNVRConnectionError(
                f"API call to {path} failed: {err}"
            ) from err

    async def async_logout(self) -> None:
        """Logout from the NVR."""
        if self._authenticated:
            try:
                await self.async_api_call(API_LOGOUT)
            except (RaySharpNVRAuthError, RaySharpNVRConnectionError):
                _LOGGER.debug("Logout request failed, ignoring")
            finally:
                self._authenticated = False
                self._csrf_token = None
                self._session_cookie = None
                self._digest_challenge = None

    async def async_close(self) -> None:
        """Close the client session."""
        await self.async_logout()
        if self._session and not self._external_session:
            await self._session.close()
            self._session = None
