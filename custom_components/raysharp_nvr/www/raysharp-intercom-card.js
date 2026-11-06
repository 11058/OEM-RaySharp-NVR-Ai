// «Домофон» card: the door panel's live video (HA's WebRTC player) and one big
// button that switches the microphone on and off (HA 2026.11 two-way audio).
// Leaving the page or putting the app in the background stops the microphone
// and the video: the door panel serves only a few streams at a time.
//
//   type: custom:raysharp-intercom-card
//   entity: camera.ch1_cam01                     # the door panel camera
//   ring: binary_sensor.ch1_cam01_doorbell        # optional: «Звонок!» banner
//   snapshot: image.ch1_cam01_doorbell_snapshot   # optional: picture of the last ring
//   name: Калитка                                 # optional
(() => {
  if (customElements.get("raysharp-intercom-card")) return;

  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  class RaySharpIntercomCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._onVisibility = () => this._visibility();
      this._onHide = () => this._stop("hidden");
    }

    setConfig(config) {
      if (!config || !config.entity) throw new Error("Укажите entity: камеру вызывной панели");
      this._config = config;
      this._built = false;
      if (this.isConnected) this._build();
    }

    getCardSize() { return 8; }
    getGridOptions() { return { columns: "full", rows: 8 }; }
    static getStubConfig(hass) {
      const cam = Object.keys(hass.states).find((e) => e.startsWith("camera."));
      return { entity: cam || "camera.ch1_cam01" };
    }

    set hass(h) {
      this._hass = h;
      if (!this._built) this._build();
      if (this._stream) this._stream.hass = h;
      this._update();
    }

    connectedCallback() {
      document.addEventListener("visibilitychange", this._onVisibility);
      window.addEventListener("pagehide", this._onHide);
      window.addEventListener("blur", this._onBlurCheck = () => setTimeout(() => this._visibility(), 300));
      if (this._config && this._hass && !this._stream && document.visibilityState === "visible") this._start();
    }

    disconnectedCallback() {
      document.removeEventListener("visibilitychange", this._onVisibility);
      window.removeEventListener("pagehide", this._onHide);
      if (this._onBlurCheck) window.removeEventListener("blur", this._onBlurCheck);
      this._stop("closed");
    }

    _build() {
      if (!this._config || !this._hass) return;
      this._built = true;
      this.shadowRoot.innerHTML = `<style>${STYLE}</style>
        <ha-card>
          <div class="head"><span class="name"></span><span class="ring" hidden>🔔 Звонок!</span></div>
          <div class="video"><div class="placeholder">Подключаюсь к панели…</div></div>
          <div class="controls">
            <button class="mic" disabled><span class="ico">🎙</span><span class="lbl">Говорить</span></button>
            <div class="state"></div>
          </div>
          <div class="note" hidden></div>
          <div class="snap" hidden><div class="sub">Снимок последнего звонка</div><img alt=""></div>
        </ha-card>`;
      this._mic = this.shadowRoot.querySelector(".mic");
      this._state = this.shadowRoot.querySelector(".state");
      this._note = this.shadowRoot.querySelector(".note");
      this._mic.onclick = () => this._toggle();
      if (!window.isSecureContext) {
        this._noteText("Страница открыта по http — микрофон браузер не даст. Откройте Home Assistant по https.");
      }
      if (document.visibilityState === "visible") this._start();
    }

    _noteText(t) {
      if (!this._note) return;
      this._note.hidden = !t;
      this._note.textContent = t || "";
    }

    _update() {
      if (!this._built || !this._hass) return;
      const c = this._config;
      const st = this._hass.states[c.entity];
      this.shadowRoot.querySelector(".name").textContent =
        c.name || (st && st.attributes.friendly_name) || c.entity;
      const ring = c.ring && this._hass.states[c.ring];
      this.shadowRoot.querySelector(".ring").hidden = !(ring && ring.state === "on");
      const snap = c.snapshot && this._hass.states[c.snapshot];
      const box = this.shadowRoot.querySelector(".snap");
      if (snap && snap.attributes.entity_picture) {
        const img = box.querySelector("img");
        const url = snap.attributes.entity_picture;
        if (img.dataset.src !== url) { img.dataset.src = url; img.src = url; }
        box.hidden = false;
      } else {
        box.hidden = true;
      }
    }

    _start() {
      if (this._stream || !this._hass || !this._config) return;
      const st = this._hass.states[this._config.entity];
      const box = this.shadowRoot.querySelector(".video");
      if (!st || !customElements.get("ha-camera-stream")) {
        box.innerHTML = `<div class="placeholder">Камера ${esc(this._config.entity)} не найдена</div>`;
        return;
      }
      // Only WebRTC: ha-camera-stream would also open HLS, a second stream
      // the door panel may refuse (it serves only a couple at once).
      const webrtc = customElements.get("ha-web-rtc-player");
      const el = document.createElement(webrtc ? "ha-web-rtc-player" : "ha-camera-stream");
      el.hass = this._hass;
      if (webrtc) { el.entityid = this._config.entity; el.autoplay = true; el.playsInline = true; el.fitMode = "contain"; }
      else { el.stateObj = st; el.allowExoPlayer = true; }
      el.muted = true; el.controls = false;
      const caps = (c) => {
        this._talk = !!(c && c.two_way_audio);
        this._mic.disabled = !this._talk || !window.isSecureContext;
        if (!this._talk) {
          this._noteText("Разговор с этой камерой недоступен: нужен Home Assistant 2026.11+, «Настройки → Система → Labs → Двусторонняя связь» и адрес панели в настройках интеграции RaySharp.");
        }
        this._show();
      };
      el.addEventListener("camera-capabilities-changed", (ev) => caps(ev.detail && ev.detail.capabilities));
      el.addEventListener("microphone-changed", () => this._show());
      if (webrtc) {
        this._hass.callWS({ type: "camera/capabilities", entity_id: this._config.entity })
          .then(caps, () => caps(null));
      }
      box.innerHTML = ""; box.appendChild(el);
      this._stream = el;
      this._show();
    }

    async _stop(why) {
      const el = this._stream;
      if (!el) return;
      try { if (el.microphoneTrack) await el.toggleMicrophone(); } catch (e) { /* closing anyway */ }
      this._stream = null;
      el.remove();
      const box = this.shadowRoot.querySelector(".video");
      if (box) box.innerHTML = `<div class="placeholder">${why === "hidden" ? "Видео остановлено, пока приложение свёрнуто" : ""}</div>`;
      this._show();
    }

    _visibility() {
      if (document.visibilityState === "hidden") this._stop("hidden");
      else if (this.isConnected) this._start();
    }

    async _toggle() {
      const el = this._stream;
      if (!el) return;
      this._mic.disabled = true;
      try {
        if (!el.microphoneTrack) el.muted = false; // hear the visitor while talking
        await el.toggleMicrophone();
      } catch (e) {
        this._noteText(`Микрофон: ${e.message || e}`);
      }
      this._mic.disabled = !this._talk;
      setTimeout(() => this._show(), 300);
      setTimeout(() => this._show(), 1500);
    }

    _show() {
      if (!this._mic) return;
      const on = !!(this._stream && this._stream.microphoneTrack);
      this._mic.classList.toggle("on", on);
      this._mic.querySelector(".ico").textContent = on ? "⏹" : "🎙";
      this._mic.querySelector(".lbl").textContent = on ? "Закончить" : "Говорить";
      this._state.textContent = !this._stream ? ""
        : on ? "● Вас слышно у калитки. Пока вы говорите, микрофон панели приглушён."
        : this._talk ? "Нажмите, чтобы ответить" : "";
    }
  }

  const STYLE = `
    ha-card { overflow: hidden; }
    .head { display:flex; align-items:center; gap: 10px; padding: 12px 16px 8px; font-size: 18px; font-weight: 500; }
    .ring { color: #fff; background: var(--error-color, #c62828); border-radius: 12px; padding: 2px 10px; font-size: 14px;
      animation: blink 1s infinite; }
    @keyframes blink { 50% { opacity: .55; } }
    .video { background: #000; min-height: 220px; display:flex; align-items:center; justify-content:center; }
    .video ha-camera-stream { width: 100%; display:block; }
    .placeholder { color: #bbb; padding: 40px 16px; text-align: center; font-size: 14px; }
    .controls { display:flex; flex-direction: column; align-items: center; gap: 8px; padding: 18px 16px 10px; }
    .mic { width: 112px; height: 112px; border-radius: 50%; border: none; cursor: pointer; display:flex; flex-direction: column;
      align-items:center; justify-content:center; gap: 4px; font-size: 15px; color: #fff; background: var(--success-color, #2e7d32);
      box-shadow: 0 4px 14px rgba(0,0,0,.3); touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
    .mic .ico { font-size: 38px; line-height: 1; }
    .mic.on { background: var(--error-color, #c62828); animation: pulse 1.4s infinite; }
    @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(198,40,40,.6); } 70% { box-shadow: 0 0 0 18px rgba(198,40,40,0); } 100% { box-shadow: 0 0 0 0 rgba(198,40,40,0); } }
    .mic:disabled { background: var(--disabled-text-color, #888); cursor: default; animation: none; }
    .state { font-size: 13px; color: var(--secondary-text-color); text-align: center; min-height: 18px; }
    .note { margin: 0 16px 12px; padding: 8px 12px; border-radius: 8px; font-size: 13px;
      border-left: 4px solid var(--warning-color, #ffa600); background: var(--secondary-background-color); }
    .snap { padding: 0 16px 16px; }
    .snap img { width: 100%; max-width: 420px; border-radius: 8px; display:block; margin-top: 6px; }
    .sub { font-size: 12px; color: var(--secondary-text-color); }
  `;

  customElements.define("raysharp-intercom-card", RaySharpIntercomCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "raysharp-intercom-card",
    name: "Домофон (RaySharp)",
    description: "Видео вызывной панели и одна кнопка, чтобы ответить голосом.",
  });
})();
