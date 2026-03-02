"""ctypes wrapper for libSESDKWrapper.so — RaySharp two-way audio (DualTalk).

Library placement
-----------------
Place the native .so files under the integration's ``lib/`` sub-directory,
organised by architecture:

    custom_components/raysharp_nvr/lib/
        arm64/                  # Raspberry Pi 4 / Linux ARM64
            libSESDKWrapper.so  # from SDK_V2.0.0/android/…/SESDKWrapper/Lib/arm64-v8a/
            libSENet.so         # from SDK_V2.0.0/android/…/SENet/Lib/arm64-v8a/
            libIOTCAPIs.so
            libP2PTunnelAPIs.so
            libRDTAPIs.so
            libTUTKGlobalAPIs.so
            libt2u.so
            libjson-c.so
        x86_64/                 # x86_64 Linux (Intel NUC, VM, …)
            libSESDKWrapper.so  # from SDK_V2.0.0/ubuntu64/bin/
            libSENet.so
            libIOTCAPIs.so
            libP2PTunnelAPIs.so
            libRDTAPIs.so
            libTUTKGlobalAPIs.so

Audio protocol
--------------
* Outgoing (HA mic → NVR):  raw 16-bit LE PCM, 8 kHz, mono, 160 bytes/chunk.
  The SDK encodes to G.711A before sending to the NVR.

* Incoming (NVR door panel audio):  received as G.711A-encoded frames in the
  preview ``frame_data_callback``.  This module decodes them to raw PCM16 and
  puts them into ``audio_queue`` for the WebSocket relay.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import platform
import struct
from pathlib import Path
from typing import Callable

_LOGGER = logging.getLogger(__name__)

# ── Audio constants ────────────────────────────────────────────────────────────
AUDIO_SAMPLE_RATE = 8000
AUDIO_CHANNELS = 1
AUDIO_BITS = 16
# 160 bytes = 80 samples × 2 bytes = 10 ms of 8 kHz 16-bit mono PCM
AUDIO_CHUNK_BYTES = 160

# ── SE frame constants ─────────────────────────────────────────────────────────
SE_FRAME_AUDIO: int = 0x41        # ord('A')
SE_ENCODE_G711A: int = 0x01

# ── Process-level singleton ───────────────────────────────────────────────────
_sdk_lib: ctypes.CDLL | None = None
_sdk_initialized: bool = False


# ── G.711A (a-law) decoder ─────────────────────────────────────────────────────
def _build_alaw_table() -> list[int]:
    """Build look-up table for G.711A (a-law) → 16-bit linear PCM."""
    table: list[int] = []
    for raw in range(256):
        a = raw ^ 0xD5          # XOR mask used in G.711A
        sign = a & 0x80
        exp = (a & 0x70) >> 4
        mant = a & 0x0F
        if exp == 0:
            linear = (mant << 1) | 1
        else:
            linear = ((mant | 0x10) << 1) | 1
            linear <<= exp - 1
        if sign == 0:
            linear = -linear
        # clamp to int16 range
        table.append(max(-32768, min(32767, linear)))
    return table

_ALAW_TABLE: list[int] = _build_alaw_table()


def decode_g711a(data: bytes) -> bytes:
    """Decode G.711A bytes → 16-bit LE signed PCM bytes."""
    return struct.pack(f"<{len(data)}h", *(_ALAW_TABLE[b] for b in data))


# ── ctypes callback types ─────────────────────────────────────────────────────
_CONN_CB   = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p)
_ALARM_CB  = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p)
_TALK_CB   = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p)
_FRAME_CB  = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p)
_PREVIEW_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p)


# ── ctypes structures ─────────────────────────────────────────────────────────

class _SEFrameHead(ctypes.Structure):
    """SEFrameHead from SEMedia.h — prepended to every audio/video frame."""
    _fields_ = [
        ("FrameTag",    ctypes.c_uint32),
        ("FrameType",   ctypes.c_uint8),
        ("EncodeType",  ctypes.c_uint8),
        ("Channel",     ctypes.c_uint8),
        ("Flags",       ctypes.c_uint8),
        ("FrameNo",     ctypes.c_uint32),
        ("FrameSize",   ctypes.c_uint32),
        ("Time",        ctypes.c_uint64),
        ("Pts",         ctypes.c_uint64),
        ("_head_union", ctypes.c_uint64),   # SEAudioHead / SEVideoHead (8 bytes each)
    ]

_SIZEOF_FRAME_HEAD: int = ctypes.sizeof(_SEFrameHead)


class _TalkSendParam(ctypes.Structure):
    """talk_send_record_data_param from SESDKWrapper.h."""
    _fields_ = [
        ("sess",                 ctypes.c_void_p),
        ("raw_pcm_data",         ctypes.c_char_p),
        ("raw_pcm_data_length",  ctypes.c_int),
        ("reserve",              ctypes.c_char * 256),
    ]


class _PreviewParam(ctypes.Structure):
    """preview_param from SESDKWrapper.h (64-bit layout).

    Natural C alignment: after ``background_picture_size`` (int, 4 bytes) the
    compiler inserts 4 bytes of padding before the next pointer field.
    ctypes applies the same rule automatically when _pack_ is not set.
    """
    _fields_ = [
        ("dev",                      ctypes.c_void_p),
        ("param",                    ctypes.c_char_p),
        ("window",                   ctypes.c_void_p),   # HWND = void* on Linux
        ("preview_cb",               _PREVIEW_CB),
        ("preview_user_param",       ctypes.c_void_p),
        ("draw_cb",                  ctypes.c_void_p),   # NULL
        ("picture_cb",               ctypes.c_void_p),   # NULL
        ("background_picture",       ctypes.c_char_p),   # NULL
        ("background_picture_size",  ctypes.c_int),      # 0
        # 4 bytes padding (auto) to align next pointer to 8-byte boundary
        ("zoom_cb",                  ctypes.c_void_p),   # NULL
        ("video_decode_cb",          ctypes.c_void_p),   # NULL
        ("video_render_cb",          ctypes.c_void_p),   # NULL
        ("audio_decode_cb",          ctypes.c_void_p),   # NULL
        ("audio_render_cb",          ctypes.c_void_p),   # NULL
        ("frame_data_cb",            _FRAME_CB),
        ("reserve",                  ctypes.c_char * 180),
    ]


# ── Library loader ─────────────────────────────────────────────────────────────

def _detect_lib_dir(base: Path) -> Path | None:
    """Return the lib sub-directory matching the host CPU architecture."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine.startswith("armv7") or machine == "armhf":
        arch = "arm32"
    else:
        _LOGGER.warning("RaySharp talk: unsupported host architecture '%s'", machine)
        return None
    d = base / "lib" / arch
    return d if d.is_dir() else None


_DEP_ORDER = [
    "libjson-c.so",
    "libTUTKGlobalAPIs.so",
    "libIOTCAPIs.so",
    "libRDTAPIs.so",
    "libt2u.so",
    "libP2PTunnelAPIs.so",
    "libSENet.so",
]


def _load_sdk(base: Path) -> ctypes.CDLL | None:
    """Load libSESDKWrapper.so and its dependencies.  Returns None on failure."""
    lib_dir = _detect_lib_dir(base)
    if lib_dir is None:
        return None

    # Load dependencies first (RTLD_GLOBAL so symbols are visible globally)
    for dep in _DEP_ORDER:
        dep_path = lib_dir / dep
        if dep_path.exists():
            try:
                ctypes.CDLL(str(dep_path), ctypes.RTLD_GLOBAL)
                _LOGGER.debug("Loaded %s", dep)
            except OSError as err:
                _LOGGER.debug("Skipped %s: %s", dep, err)

    wrapper = lib_dir / "libSESDKWrapper.so"
    if not wrapper.exists():
        _LOGGER.error(
            "RaySharp talk: libSESDKWrapper.so not found in %s. "
            "See integration docs for library placement instructions.",
            lib_dir,
        )
        return None

    try:
        lib = ctypes.CDLL(str(wrapper))
        _LOGGER.info("RaySharp talk: loaded libSESDKWrapper.so from %s", lib_dir)
        return lib
    except OSError as err:
        _LOGGER.error("RaySharp talk: failed to load libSESDKWrapper.so: %s", err)
        return None


def _configure_lib(lib: ctypes.CDLL) -> None:
    """Set argtypes/restype for every SDK function we use."""
    # se_sdk_wrapper_init(const char* param) -> int
    lib.se_sdk_wrapper_init.argtypes = [ctypes.c_char_p]
    lib.se_sdk_wrapper_init.restype = ctypes.c_int
    # se_sdk_wrapper_uninit(const char* param) -> int
    lib.se_sdk_wrapper_uninit.argtypes = [ctypes.c_char_p]
    lib.se_sdk_wrapper_uninit.restype = ctypes.c_int
    # se_create_device() -> void*
    lib.se_create_device.argtypes = []
    lib.se_create_device.restype = ctypes.c_void_p
    # se_destroy_device(void* dev) -> int
    lib.se_destroy_device.argtypes = [ctypes.c_void_p]
    lib.se_destroy_device.restype = ctypes.c_int
    # se_device_login(dev, param, conn_cb, alarm_cb, user_param) -> int
    lib.se_device_login.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p,
        _CONN_CB, _ALARM_CB, ctypes.c_void_p,
    ]
    lib.se_device_login.restype = ctypes.c_int
    # se_device_logout(dev) -> int
    lib.se_device_logout.argtypes = [ctypes.c_void_p]
    lib.se_device_logout.restype = ctypes.c_int
    # se_start_talk_to_channel(dev, param, talk_cb, user_param) -> void*
    lib.se_start_talk_to_channel.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, _TALK_CB, ctypes.c_void_p,
    ]
    lib.se_start_talk_to_channel.restype = ctypes.c_void_p
    # se_stop_talk(sess) -> int
    lib.se_stop_talk.argtypes = [ctypes.c_void_p]
    lib.se_stop_talk.restype = ctypes.c_int
    # se_talk_send_record_data(const param*) -> int
    lib.se_talk_send_record_data.argtypes = [ctypes.POINTER(_TalkSendParam)]
    lib.se_talk_send_record_data.restype = ctypes.c_int
    # se_start_preview(const preview_param*) -> void*
    lib.se_start_preview.argtypes = [ctypes.POINTER(_PreviewParam)]
    lib.se_start_preview.restype = ctypes.c_void_p
    # se_stop_preview(sess) -> int
    lib.se_stop_preview.argtypes = [ctypes.c_void_p]
    lib.se_stop_preview.restype = ctypes.c_int


# ── Main class ─────────────────────────────────────────────────────────────────

class RaySharpTalkClient:
    """Manages a two-way audio session with the RaySharp NVR via native SDK.

    Lifecycle
    ---------
    1. ``initialize()``    — load .so + call ``se_sdk_wrapper_init`` (once per process).
    2. ``connect()``       — ``se_create_device`` + ``se_device_login``.
    3. ``start_talk(ch)``  — open talk session (send path).
    4. ``start_preview(ch)`` — open preview session (receive path).
    5. Send loop: ``send_pcm(raw_pcm_bytes)``.
    6. Receive loop: await ``audio_queue.get()`` → raw PCM16 bytes.
    7. ``stop()``          — tear down all sessions; call from executor.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        lib_base: Path,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._lib_base = lib_base
        self._loop = loop

        self._lib: ctypes.CDLL | None = None
        self._dev: int | None = None           # opaque device_id (c_void_p)
        self._talk_sess: int | None = None     # opaque session_id
        self._preview_sess: int | None = None

        # Thread-safe queue for decoded PCM16 audio from NVR → browser
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)

        # Keep ctypes callback objects alive (must not be GC'd)
        self._conn_cb: _CONN_CB | None = None
        self._alarm_cb: _ALARM_CB | None = None
        self._talk_cb: _TALK_CB | None = None
        self._frame_cb: _FRAME_CB | None = None
        self._preview_cb: _PREVIEW_CB | None = None

        self._stopped = False

    # ── Public (call from executor thread) ────────────────────────────────────

    def initialize(self) -> bool:
        """Load the shared library and initialise the SDK (once per process)."""
        global _sdk_lib, _sdk_initialized

        if _sdk_lib is None:
            lib = _load_sdk(self._lib_base)
            if lib is None:
                return False
            _configure_lib(lib)
            _sdk_lib = lib

        self._lib = _sdk_lib

        if not _sdk_initialized:
            param = json.dumps({"se iot router": "smartercloud.cc"})
            ret = self._lib.se_sdk_wrapper_init(param.encode())
            if ret != 0:
                _LOGGER.error("se_sdk_wrapper_init failed: %d", ret)
                return False
            _sdk_initialized = True
            _LOGGER.debug("se_sdk_wrapper_init OK")

        return True

    def connect(self) -> bool:
        """Create device handle and login to the NVR."""
        if self._lib is None:
            return False

        self._dev = self._lib.se_create_device()
        if not self._dev:
            _LOGGER.error("se_create_device returned NULL")
            return False

        param = json.dumps({
            "ip or id": self._host,
            "port": self._port,
            "user": self._username,
            "password": self._password,
            "p2p type": "ip",
            "oem type": "default",
            "protocol": "http",
        })

        self._conn_cb = _CONN_CB(self._on_connection)
        self._alarm_cb = _ALARM_CB(self._on_alarm)

        ret = self._lib.se_device_login(
            self._dev,
            param.encode(),
            self._conn_cb,
            self._alarm_cb,
            None,
        )
        if ret != 0:
            _LOGGER.error("se_device_login failed: %d", ret)
            return False

        _LOGGER.debug("se_device_login OK for %s", self._host)
        return True

    def start_talk(self, channel: int) -> bool:
        """Open the outgoing talk channel (HA mic → NVR).

        Args:
            channel: 1-based channel number as used in HA.
        """
        if self._lib is None or self._dev is None:
            return False

        param = json.dumps({
            "channel": channel - 1,    # SDK uses 0-based index
            "audio format": "g711a",
            "talk mode": "full duplex",
        })

        self._talk_cb = _TALK_CB(self._on_talk_event)
        sess = self._lib.se_start_talk_to_channel(
            self._dev,
            param.encode(),
            self._talk_cb,
            None,
        )
        if not sess:
            _LOGGER.error("se_start_talk_to_channel returned NULL (ch %d)", channel)
            return False

        self._talk_sess = sess
        _LOGGER.debug("Talk session started on channel %d", channel)
        return True

    def start_preview(self, channel: int) -> bool:
        """Open the incoming audio stream (NVR door panel → HA).

        Uses the preview API with a frame_data_callback to intercept raw
        audio frames.  The frame callback decodes G.711A → PCM16 and posts
        the result to ``audio_queue``.

        Args:
            channel: 1-based channel number.
        """
        if self._lib is None or self._dev is None:
            return False

        param = json.dumps({
            "channel": channel - 1,
            "stream type": "sub stream",
            "auto connect": True,
        })

        self._preview_cb = _PREVIEW_CB(self._on_preview_event)
        self._frame_cb = _FRAME_CB(self._on_audio_frame)

        pp = _PreviewParam()
        pp.dev = self._dev
        pp.param = param.encode()
        pp.window = 1          # non-NULL fake HWND for headless
        pp.preview_cb = self._preview_cb
        pp.preview_user_param = None
        pp.draw_cb = None
        pp.picture_cb = None
        pp.background_picture = None
        pp.background_picture_size = 0
        pp.zoom_cb = None
        pp.video_decode_cb = None
        pp.video_render_cb = None
        pp.audio_decode_cb = None
        pp.audio_render_cb = None
        pp.frame_data_cb = self._frame_cb

        sess = self._lib.se_start_preview(ctypes.byref(pp))
        if not sess:
            _LOGGER.error("se_start_preview returned NULL (ch %d)", channel)
            return False

        self._preview_sess = sess
        _LOGGER.debug("Preview session started on channel %d", channel)
        return True

    def send_pcm(self, pcm_data: bytes) -> None:
        """Send a raw PCM16 chunk (160 bytes / 10 ms) to the NVR.

        The SDK encodes it to G.711A internally before transmission.
        Call this from an executor thread (blocking C call).
        """
        if self._lib is None or self._talk_sess is None or self._stopped:
            return

        buf = ctypes.create_string_buffer(pcm_data)
        p = _TalkSendParam()
        p.sess = self._talk_sess
        p.raw_pcm_data = buf
        p.raw_pcm_data_length = len(pcm_data)

        ret = self._lib.se_talk_send_record_data(ctypes.byref(p))
        if ret != 0:
            _LOGGER.debug("se_talk_send_record_data returned %d", ret)

    def stop(self) -> None:
        """Tear down all sessions and logout.  Call from executor thread."""
        self._stopped = True

        if self._lib is None:
            return

        if self._talk_sess:
            self._lib.se_stop_talk(self._talk_sess)
            self._talk_sess = None

        if self._preview_sess:
            self._lib.se_stop_preview(self._preview_sess)
            self._preview_sess = None

        if self._dev:
            self._lib.se_device_logout(self._dev)
            self._lib.se_destroy_device(self._dev)
            self._dev = None

        _LOGGER.debug("RaySharpTalkClient stopped")

    # ── C callbacks (called from SDK internal threads) ────────────────────────

    def _on_connection(self, param: bytes, _user: None) -> None:
        info = param.decode(errors="replace") if param else ""
        _LOGGER.debug("SDK connection event: %s", info)

    def _on_alarm(self, alarm_type: bytes, param: bytes, _user: None) -> None:
        _LOGGER.debug("SDK alarm %s: %s", alarm_type, param)

    def _on_talk_event(self, param: bytes, _user: None) -> None:
        info = param.decode(errors="replace") if param else ""
        _LOGGER.debug("SDK talk event: %s", info)

    def _on_preview_event(self, param: bytes, _user: None) -> None:
        info = param.decode(errors="replace") if param else ""
        _LOGGER.debug("SDK preview event: %s", info)

    def _on_audio_frame(
        self,
        frame_ptr: int,
        size: int,
        _user: None,
    ) -> int:
        """frame_data_callback — called from SDK C thread for each NVR frame.

        Extracts G.711A audio data, decodes to PCM16, and posts it to the
        asyncio queue via ``call_soon_threadsafe``.
        """
        if self._stopped or not frame_ptr or size < _SIZEOF_FRAME_HEAD:
            return 0

        try:
            head = _SEFrameHead.from_address(frame_ptr)
            if head.FrameType != SE_FRAME_AUDIO:
                return 0
            if head.EncodeType != SE_ENCODE_G711A:
                # Only G.711A supported; other codecs ignored
                return 0

            audio_size = head.FrameSize - _SIZEOF_FRAME_HEAD
            if audio_size <= 0:
                return 0

            # Copy G.711A bytes out of the SDK's buffer
            g711a = bytes(
                (ctypes.c_uint8 * audio_size).from_address(
                    frame_ptr + _SIZEOF_FRAME_HEAD
                )
            )

            pcm16 = decode_g711a(g711a)

            # Post to asyncio event loop (thread-safe)
            try:
                self._loop.call_soon_threadsafe(
                    self._put_audio_nowait, pcm16
                )
            except RuntimeError:
                pass  # loop closed

        except Exception:  # noqa: BLE001
            pass  # never let a C callback raise

        return 0

    def _put_audio_nowait(self, pcm16: bytes) -> None:
        """Put PCM16 into the queue; silently drop if full (backpressure)."""
        try:
            self.audio_queue.put_nowait(pcm16)
        except asyncio.QueueFull:
            pass


# ── Module-level convenience ──────────────────────────────────────────────────

def sdk_available(base: Path) -> bool:
    """Return True if the native library directory exists for this host."""
    return _detect_lib_dir(base) is not None
