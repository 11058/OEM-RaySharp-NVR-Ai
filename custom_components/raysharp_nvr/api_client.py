"""API client for RaySharp NVR with HTTP Digest authentication."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from typing import Any

import aiohttp

from .const import API_EVENT_CHECK, API_HEARTBEAT, API_LOGIN, API_LOGOUT

_LOGGER = logging.getLogger(__name__)

# The firmware answers requests carrying a dead session with HTTP 400 instead
# of 401, so any non-200 may mean "session gone".  Statuses we probe on:
_SESSION_SUSPECT_STATUSES = (400, 403)
# A recent 200 from any endpoint proves the session is alive, so a 400 within
# this window is the endpoint's own doing (unsupported on this firmware) and
# must not trigger a re-login.
_SESSION_ALIVE_TTL = 10.0


class RaySharpNVRAuthError(Exception):
    """Exception for authentication errors."""


class RaySharpNVRConnectionError(Exception):
    """Exception for connection errors."""


class RaySharpNVRApiError(RaySharpNVRConnectionError):
    """The NVR answered HTTP 200 but the body says the call failed."""

    def __init__(self, path: str, reason: str, error_code: str) -> None:
        """Store the NVR's own error wording."""
        self.reason = reason
        self.error_code = error_code
        super().__init__(f"API call to {path} refused: {reason} ({error_code})")


def _raise_for_api_error(path: str, payload: Any) -> None:
    """Raise when a 200 response carries a failure envelope.

    Some endpoints report refusals in the body rather than the status line —
    /API/AI/VhdLogCount/Get answers 200 with {"result": "failed",
    "error_code": "no_permission"} for an account that lacks AI rights.  Taken
    at face value that envelope gets stored as if it were data, and the
    entities silently read empty forever.
    """
    if not isinstance(payload, dict):
        return
    # The marker sits at the top level on some endpoints, inside data on others.
    for scope in (payload, payload.get("data")):
        if not isinstance(scope, dict):
            continue
        if str(scope.get("result")).lower() in ("failed", "fail"):
            raise RaySharpNVRApiError(
                path,
                str(scope.get("reason", "unspecified")),
                str(scope.get("error_code", "unknown")),
            )


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
        # Bumped on every successful login; lets parallel callers that hit a
        # dead session recognise that someone else already renewed it.
        self._session_generation = 0
        self._last_success_at = 0.0

    @property
    def authenticated(self) -> bool:
        """Return whether the client is authenticated."""
        return self._authenticated

    def invalidate_session(self) -> None:
        """Drop the current session so the next call authenticates again."""
        self._authenticated = False
        self._last_success_at = 0.0

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
        self._session_generation += 1
        self._last_success_at = time.monotonic()
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
        except (RaySharpNVRAuthError, RaySharpNVRConnectionError) as err:
            # Session recovery inside async_api_call already tried to log in
            # again; if we still fail, drop the session so the next call (or
            # the coordinator) authenticates from scratch.
            self._authenticated = False
            _LOGGER.debug("Heartbeat failed (%s), session marked invalid", err)
            return False

    async def _async_recover_session(self, generation: int) -> bool:
        """Renew the session after a request failed with a suspect status.

        The NVR answers requests carrying an expired or evicted session with
        HTTP 400 rather than 401, so a failed call is the only hint we get.
        Probe with a heartbeat (cheapest call that needs a live session): if
        the probe succeeds the session is fine and the original failure was
        request-specific, so we leave it to the caller to raise.

        Returns True when the session was renewed and the caller should retry
        the request once.
        """
        async with self._lock:
            if generation != self._session_generation:
                # Another task re-authenticated while we waited for the lock.
                return True

            if time.monotonic() - self._last_success_at < _SESSION_ALIVE_TTL:
                # Session was verified moments ago — this endpoint is simply
                # rejecting our request; don't log in on every such call.
                return False

            try:
                await self._raw_api_call(API_HEARTBEAT)
            except (RaySharpNVRAuthError, RaySharpNVRConnectionError):
                pass  # session really is gone
            else:
                self._last_success_at = time.monotonic()
                return False

            self._authenticated = False
            try:
                await self.async_login()
            except (RaySharpNVRAuthError, RaySharpNVRConnectionError) as err:
                _LOGGER.debug("Session recovery failed: %s", err)
                return False

            _LOGGER.info("NVR session was rejected — re-authenticated")
            return True

    async def async_api_call(
        self, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make an authenticated API call to the NVR.

        Uses session cookie + CSRF token from login.
        Re-authenticates on 401 responses, and on the 400/403 the firmware
        returns for a session it no longer knows about.
        """
        session = self._get_session()
        url = f"{self._base_url}{path}"
        payload = {"version": "1.0", "data": data or {}}
        headers = self._build_headers()
        generation = self._session_generation

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

                if resp.status in _SESSION_SUSPECT_STATUSES and path != API_HEARTBEAT:
                    if await self._async_recover_session(generation):
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

                self._last_success_at = time.monotonic()
                payload = await resp.json(content_type=None)
                _raise_for_api_error(path, payload)
                return payload

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

                self._last_success_at = time.monotonic()
                payload = await resp.json(content_type=None)
                _raise_for_api_error(path, payload)
                return payload

        except aiohttp.ClientError as err:
            raise RaySharpNVRConnectionError(
                f"API call to {path} failed: {err}"
            ) from err

    async def async_event_check(
        self,
        reader_id: int | None = None,
        sequence: int | None = None,
        lap_number: int | None = None,
    ) -> dict[str, Any]:
        """Short-poll NVR /API/Event/Check for real-time alarm events.

        The NVR uses a reader_id subscription model:
        • First call  (reader_id=None): send empty body {} → NVR returns
          reader_id, sequence, lap_number and the current alarm state.
        • Subsequent  (reader_id set): send {reader_id, sequence, lap_number}
          → NVR returns new alarm events since last sequence, or
          {"heat_alarm": "HeatAlarm"} if nothing changed.

        IMPORTANT: The NVR rejects null-valued fields with HTTP 400.  So we
        omit reader_id/sequence/lap_number entirely on the first call (empty
        body) and only include them when they have real values.
        """
        if reader_id is None:
            # First call: subscribe / get initial state — empty body required.
            payload_data: dict[str, Any] = {}
        else:
            payload_data = {
                "reader_id": reader_id,
                "sequence": sequence,
                "lap_number": lap_number,
            }

        session = self._get_session()
        url = f"{self._base_url}{API_EVENT_CHECK}"
        payload = {"version": "1.0", "data": payload_data}
        headers = self._build_headers()
        generation = self._session_generation

        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    _LOGGER.debug("Event check got 401, re-logging in")
                    async with self._lock:
                        await self.async_login()
                    # Re-subscribe after re-login
                    return await self.async_event_check(None, None, None)

                if resp.status in _SESSION_SUSPECT_STATUSES:
                    if await self._async_recover_session(generation):
                        # Session renewed — the old reader_id died with it.
                        return await self.async_event_check(None, None, None)

                if resp.status != 200:
                    raise RaySharpNVRConnectionError(
                        f"Event check failed with status {resp.status}"
                    )

                csrf = (
                    resp.headers.get("X-csrftoken")
                    or resp.headers.get("X-CsrfToken")
                )
                if csrf:
                    self._csrf_token = csrf

                self._last_success_at = time.monotonic()
                payload = await resp.json(content_type=None)
                _raise_for_api_error(path, payload)
                return payload

        except asyncio.TimeoutError:
            # Socket-level timeout — return empty so the caller retries.
            return {}
        except aiohttp.ClientError as err:
            raise RaySharpNVRConnectionError(
                f"Event check connection failed: {err}"
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
