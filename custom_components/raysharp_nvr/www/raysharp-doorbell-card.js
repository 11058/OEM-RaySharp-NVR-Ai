/**
 * RaySharp Doorbell Card
 * ======================
 * Lovelace custom card for RaySharp NVR doorbell / intercom functionality.
 *
 * Features
 * --------
 * - Live camera thumbnail (HA camera entity, auto-refreshed every 5 s when idle)
 * - Doorbell ring detection via the binary_sensor.{name}_doorbell entity
 * - Answer / Hang Up buttons
 * - Full-duplex two-way audio via WebAudio API + HA WebSocket relay
 *
 * Installation
 * ------------
 * 1. The integration registers this file at /raysharp_nvr/raysharp-doorbell-card.js
 * 2. In Lovelace → Resources, add:
 *      URL:  /raysharp_nvr/raysharp-doorbell-card.js
 *      Type: JavaScript Module
 *
 * Card configuration (YAML)
 * -------------------------
 *   type: custom:raysharp-doorbell-card
 *   entry_id: <config_entry_id>   # copy from Settings → Devices → RaySharp NVR → entry id
 *   channel: 1                    # NVR channel number (1-based)
 *   camera_entity: camera.ch01    # optional — camera entity for thumbnail
 *   doorbell_entity: binary_sensor.ch01_doorbell  # optional — ring sensor
 *   title: Front Door             # optional card title
 */

const SAMPLE_RATE = 8000;       // Hz
const CHUNK_BYTES = 160;        // bytes = 80 samples × 2 bytes = 10 ms

// ── Helpers ────────────────────────────────────────────────────────────────────

/** Convert Float32 [-1,1] → Int16 LE binary. */
function float32ToInt16(float32Array) {
  const out = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i++) {
    let s = Math.max(-1, Math.min(1, float32Array[i]));
    out[i] = s < 0 ? s * 32768 : s * 32767;
  }
  return out;
}

/** Convert Int16 LE binary → Float32 [-1,1]. */
function int16ToFloat32(int16Array) {
  const out = new Float32Array(int16Array.length);
  for (let i = 0; i < int16Array.length; i++) {
    out[i] = int16Array[i] / 32768.0;
  }
  return out;
}

// ── Card definition ────────────────────────────────────────────────────────────

class RaySharpDoorbellCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._ws = null;
    this._audioCtx = null;
    this._micStream = null;
    this._micProcessor = null;
    this._micSource = null;
    this._inCall = false;
    this._ringing = false;
    this._snapshotTimer = null;
    this._nextPlayTime = 0;
    this._rendered = false;
  }

  // ── Lovelace API ────────────────────────────────────────────────────────────

  setConfig(config) {
    if (!config.entry_id) throw new Error("entry_id is required");
    if (!config.channel) throw new Error("channel is required");
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) this._render();
    this._updateDoorbell();
    this._updateSnapshot();
  }

  static getConfigElement() {
    return null; // no visual editor
  }

  static getStubConfig() {
    return { entry_id: "", channel: 1, title: "Doorbell" };
  }

  // ── Rendering ───────────────────────────────────────────────────────────────

  _render() {
    if (!this._config) return;
    this._rendered = true;
    const title = this._config.title || "Doorbell";

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card {
          overflow: hidden;
          position: relative;
        }
        .thumbnail-wrap {
          position: relative;
          background: #111;
          min-height: 120px;
          display: flex;
          align-items: center;
          justify-content: center;
        }
        .thumbnail-wrap img {
          width: 100%;
          display: block;
          object-fit: contain;
          max-height: 240px;
        }
        .no-cam {
          color: #555;
          font-size: 13px;
          padding: 24px;
          text-align: center;
        }
        .ring-badge {
          position: absolute;
          top: 8px;
          left: 8px;
          background: #f44336;
          color: #fff;
          font-size: 12px;
          font-weight: bold;
          border-radius: 12px;
          padding: 3px 10px;
          display: none;
        }
        .ring-badge.visible { display: block; }
        .controls {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 12px 16px;
          gap: 12px;
        }
        .title { font-weight: 500; font-size: 15px; flex: 1; }
        .btn {
          border: none;
          border-radius: 20px;
          padding: 8px 18px;
          font-size: 13px;
          font-weight: bold;
          cursor: pointer;
          transition: opacity 0.15s;
        }
        .btn:disabled { opacity: 0.4; cursor: default; }
        .btn-answer  { background: #4caf50; color: #fff; }
        .btn-hangup  { background: #f44336; color: #fff; display: none; }
        .status-msg {
          font-size: 12px;
          color: var(--secondary-text-color, #888);
          padding: 0 16px 10px;
          min-height: 18px;
        }
      </style>

      <ha-card>
        <div class="thumbnail-wrap">
          <img id="snapshot" alt="" style="display:none" />
          <div class="no-cam" id="no-cam">No camera configured</div>
          <div class="ring-badge" id="ring-badge">RINGING</div>
        </div>
        <div class="controls">
          <span class="title">${title}</span>
          <button class="btn btn-answer" id="btn-answer">Answer</button>
          <button class="btn btn-hangup" id="btn-hangup">Hang Up</button>
        </div>
        <div class="status-msg" id="status-msg"></div>
      </ha-card>
    `;

    this.shadowRoot.getElementById("btn-answer").addEventListener("click", () => this._answer());
    this.shadowRoot.getElementById("btn-hangup").addEventListener("click", () => this._hangUp());

    // Start snapshot refresh
    if (this._config.camera_entity) {
      this._scheduleSnapshot();
    }
  }

  // ── Camera thumbnail ────────────────────────────────────────────────────────

  _scheduleSnapshot() {
    clearTimeout(this._snapshotTimer);
    this._snapshotTimer = setInterval(() => this._updateSnapshot(), 5000);
  }

  _updateSnapshot() {
    if (!this._hass || !this._config.camera_entity || this._inCall) return;
    const token = this._hass.auth.data.access_token;
    const entity = this._config.camera_entity;
    const img = this.shadowRoot?.getElementById("snapshot");
    const noC = this.shadowRoot?.getElementById("no-cam");
    if (!img) return;
    // HA camera proxy endpoint
    const url = `/api/camera_proxy/${entity}?access_token=${token}&t=${Date.now()}`;
    img.onload = () => {
      img.style.display = "block";
      if (noC) noC.style.display = "none";
    };
    img.onerror = () => {
      img.style.display = "none";
      if (noC) noC.style.display = "flex";
    };
    img.src = url;
  }

  // ── Doorbell state ──────────────────────────────────────────────────────────

  _updateDoorbell() {
    if (!this._hass || !this._config.doorbell_entity) return;
    const state = this._hass.states[this._config.doorbell_entity];
    const ringing = state?.state === "on";
    const badge = this.shadowRoot?.getElementById("ring-badge");
    if (!badge) return;
    if (ringing !== this._ringing) {
      this._ringing = ringing;
      badge.classList.toggle("visible", ringing);
      if (ringing) this._setStatus("Doorbell ringing…");
      else if (!this._inCall) this._setStatus("");
    }
  }

  // ── Two-way audio ───────────────────────────────────────────────────────────

  async _answer() {
    if (this._inCall) return;
    this._setStatus("Connecting…");

    // Request microphone access
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: SAMPLE_RATE,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
    } catch (e) {
      this._setStatus("Microphone access denied");
      return;
    }

    // Build HA WebSocket URL
    const entryId = this._config.entry_id;
    const channel = this._config.channel;
    const token = this._hass.auth.data.access_token;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${location.host}/api/raysharp_nvr/talk/${entryId}/${channel}?access_token=${token}`;

    let ws;
    try {
      ws = new WebSocket(wsUrl);
      ws.binaryType = "arraybuffer";
    } catch (e) {
      this._setStatus("WebSocket error: " + e.message);
      stream.getTracks().forEach(t => t.stop());
      return;
    }

    ws.onopen = () => {
      this._setStatus("In call");
      this._setCallState(true);
      this._startMic(ws, stream);
    };

    ws.onmessage = (evt) => {
      if (evt.data instanceof ArrayBuffer) {
        this._playPCM(evt.data);
      }
    };

    ws.onerror = () => {
      this._setStatus("Connection error");
      this._cleanup();
    };

    ws.onclose = () => {
      if (this._inCall) {
        this._setStatus("Call ended");
        this._cleanup();
      }
    };

    this._ws = ws;
    this._micStream = stream;
  }

  _hangUp() {
    if (!this._inCall) return;
    this._setStatus("Call ended");
    this._cleanup();
  }

  _startMic(ws, stream) {
    // Create AudioContext at 8 kHz
    this._audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: SAMPLE_RATE,
    });
    this._nextPlayTime = this._audioCtx.currentTime;

    const source = this._audioCtx.createMediaStreamSource(stream);
    // ScriptProcessor: bufferSize=256 gives ~32 ms at 8 kHz
    // We'll accumulate and send in 160-byte chunks
    const processor = this._audioCtx.createScriptProcessor(256, 1, 1);
    let sendBuf = new Int16Array(0);

    processor.onaudioprocess = (e) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = float32ToInt16(f32);
      // Concatenate into send buffer
      const merged = new Int16Array(sendBuf.length + i16.length);
      merged.set(sendBuf);
      merged.set(i16, sendBuf.length);
      sendBuf = merged;
      // Flush in CHUNK_BYTES (160 byte = 80 int16 samples) chunks
      const samplesPerChunk = CHUNK_BYTES / 2;
      while (sendBuf.length >= samplesPerChunk) {
        const chunk = sendBuf.slice(0, samplesPerChunk);
        sendBuf = sendBuf.slice(samplesPerChunk);
        ws.send(chunk.buffer);
      }
    };

    source.connect(processor);
    processor.connect(this._audioCtx.destination);
    this._micSource = source;
    this._micProcessor = processor;
  }

  _playPCM(arrayBuffer) {
    if (!this._audioCtx) return;
    const i16 = new Int16Array(arrayBuffer);
    const f32 = int16ToFloat32(i16);

    const audioBuf = this._audioCtx.createBuffer(1, f32.length, SAMPLE_RATE);
    audioBuf.getChannelData(0).set(f32);

    const src = this._audioCtx.createBufferSource();
    src.buffer = audioBuf;
    src.connect(this._audioCtx.destination);

    // Schedule playback seamlessly (avoid gaps/overlaps)
    const now = this._audioCtx.currentTime;
    if (this._nextPlayTime < now) this._nextPlayTime = now;
    src.start(this._nextPlayTime);
    this._nextPlayTime += audioBuf.duration;
  }

  _cleanup() {
    this._inCall = false;
    this._setCallState(false);

    if (this._ws) {
      try { this._ws.close(); } catch (_) {}
      this._ws = null;
    }
    if (this._micProcessor) {
      try { this._micProcessor.disconnect(); } catch (_) {}
      this._micProcessor = null;
    }
    if (this._micSource) {
      try { this._micSource.disconnect(); } catch (_) {}
      this._micSource = null;
    }
    if (this._micStream) {
      this._micStream.getTracks().forEach(t => t.stop());
      this._micStream = null;
    }
    if (this._audioCtx) {
      try { this._audioCtx.close(); } catch (_) {}
      this._audioCtx = null;
    }
    this._nextPlayTime = 0;
  }

  // ── UI helpers ──────────────────────────────────────────────────────────────

  _setCallState(inCall) {
    this._inCall = inCall;
    const btnAnswer = this.shadowRoot?.getElementById("btn-answer");
    const btnHangup = this.shadowRoot?.getElementById("btn-hangup");
    if (!btnAnswer || !btnHangup) return;
    btnAnswer.style.display = inCall ? "none" : "inline-block";
    btnHangup.style.display = inCall ? "inline-block" : "none";
  }

  _setStatus(msg) {
    const el = this.shadowRoot?.getElementById("status-msg");
    if (el) el.textContent = msg;
  }

  // ── Lifecycle ───────────────────────────────────────────────────────────────

  disconnectedCallback() {
    clearInterval(this._snapshotTimer);
    this._cleanup();
  }
}

customElements.define("raysharp-doorbell-card", RaySharpDoorbellCard);

// Register with Lovelace card picker
window.customCards = window.customCards || [];
window.customCards.push({
  type: "raysharp-doorbell-card",
  name: "RaySharp Doorbell",
  description: "Two-way audio intercom for RaySharp NVR doorbell channels",
  preview: false,
});
