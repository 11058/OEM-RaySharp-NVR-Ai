// RaySharp door panel as a classic intercom, in every Home Assistant screen.
//
// 1) A watcher that runs on every HA page (the integration loads this file
//    everywhere): it follows the intercom state; on a ring it opens the
//    «Домофон» dashboard (per device, can be switched off), plays a ringtone
//    while the page is visible, and goes back after the call.
// 2) The card: the panel's live video (WebRTC only — the panel serves very
//    few streams; go2rtc keeps one for all screens) and one button:
//    Ответить / Говорить → Закончить. Only one screen can talk; the others
//    show who answered. Hiding the page ends your call and stops the video.
//
//   type: custom:raysharp-intercom-card
//   entity: camera.ch1_cam01          # door panel camera
//   snapshot: image.ch1_cam01_doorbell_snapshot   # optional
//   name: Калитка                     # optional
(() => {
  if (window.__raysharpIntercom) return;

  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const store = {
    get(k, d) { try { const v = localStorage.getItem(`raysharp_intercom_${k}`); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(`raysharp_intercom_${k}`, String(v)); } catch (e) { /* private mode */ } },
  };
  const clientId = (() => {
    try {
      let id = sessionStorage.getItem("raysharp_intercom_client");
      if (!id) { id = Math.random().toString(36).slice(2, 12); sessionStorage.setItem("raysharp_intercom_client", id); }
      return id;
    } catch (e) { return Math.random().toString(36).slice(2, 12); }
  })();
  function defaultLabel() {
    const ua = navigator.userAgent || "";
    const app = /Home Assistant\//.test(ua) ? " · приложение" : "";
    const dev = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android"
      : /Mac OS X/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "браузер";
    return dev + app;
  }
  const label = () => store.get("label", "") || defaultLabel();

  // --- ringtone ----------------------------------------------------------------
  // Classic two-note door chime, repeated. Browsers allow sound only after the
  // user touched the page once; we unlock on the first touch/click anywhere.
  let ctx = null;
  const unlock = () => {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === "suspended") ctx.resume();
    } catch (e) { /* no audio */ }
  };
  ["pointerdown", "keydown", "touchstart"].forEach((ev) => window.addEventListener(ev, unlock, { capture: true, passive: true }));
  let ringTimer = null;
  function chime() {
    if (!ctx || ctx.state !== "running") return false;
    const t = ctx.currentTime;
    [[659, 0], [523, 0.45]].forEach(([f, d]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t + d);
      g.gain.exponentialRampToValueAtTime(0.35, t + d + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + d + 0.9);
      o.connect(g).connect(ctx.destination); o.start(t + d); o.stop(t + d + 1);
    });
    return true;
  }
  function startRing() {
    if (ringTimer) return;
    unlock();
    const tick = () => { if (document.visibilityState === "visible") chime(); };
    tick();
    ringTimer = setInterval(tick, 2500);
  }
  function stopRing() { clearInterval(ringTimer); ringTimer = null; }

  // --- the watcher ---------------------------------------------------------------
  const W = {
    state: new Map(), // key -> intercom view
    cards: new Set(),
    backTo: null,
    hass: null,
    get primary() { return [...this.state.values()][0] || null; },
    emit() { this.cards.forEach((c) => c._intercom(this.primary)); },
  };
  window.__raysharpIntercom = W;

  function navigate(path) {
    history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }

  function onState(v) {
    if (!v) return;
    const prev = W.state.get(v.key);
    W.state.set(v.key, v);
    const was = prev ? prev.state : "idle";
    if (v.state === "ringing") {
      startRing();
      if (was !== "ringing" && store.get("autoswitch", "1") === "1" && document.visibilityState === "visible"
          && !location.pathname.startsWith(v.dashboard)) {
        W.backTo = location.pathname + location.search;
        navigate(v.dashboard);
      }
    } else {
      stopRing();
    }
    if (v.state === "idle" && W.backTo && was !== "idle") {
      const back = W.backTo;
      W.backTo = null;
      setTimeout(() => {
        const now = W.state.get(v.key);
        if (now && now.state === "idle" && location.pathname.startsWith(v.dashboard)) navigate(back);
      }, 4000);
    }
    W.emit();
  }

  async function start() {
    const ha = document.querySelector("home-assistant");
    const hass = ha && ha.hass;
    if (!hass || !hass.connection) { setTimeout(start, 1000); return; }
    W.hass = hass;
    try {
      W.unsub = await hass.connection.subscribeMessage((ev) => {
        if (ev && ev.intercom) onState(ev.intercom);
      }, { type: "raysharp_nvr/intercom/subscribe", client: clientId });
      const r = await hass.callWS({ type: "raysharp_nvr/intercom/state" });
      (r && r.intercoms || []).forEach(onState);
    } catch (e) { /* integration without an intercom */ }
  }
  start();

  W.answer = async (key) => {
    const hass = (document.querySelector("home-assistant") || {}).hass || W.hass;
    return hass.callWS({ type: "raysharp_nvr/intercom/answer", key, client: clientId, label: label() });
  };
  W.hangup = async (key) => {
    const hass = (document.querySelector("home-assistant") || {}).hass || W.hass;
    return hass.callWS({ type: "raysharp_nvr/intercom/hangup", key, client: clientId }).catch(() => {});
  };
  W.mine = (v) => !!(v && v.state === "talking" && v.answered_by && v.answered_by.id === clientId);

  // --- the card ------------------------------------------------------------------
  class RaySharpIntercomCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._onVisibility = () => this._visibility();
    }

    setConfig(config) {
      if (!config || !config.entity) throw new Error("Укажите entity: камеру вызывной панели");
      this._config = config;
      this._built = false;
      if (this.isConnected && this._hass) this._build();
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
      W.cards.add(this);
      document.addEventListener("visibilitychange", this._onVisibility);
      if (this._built && !this._stream && document.visibilityState === "visible") this._start();
      this._intercom(W.primary);
    }

    disconnectedCallback() {
      W.cards.delete(this);
      document.removeEventListener("visibilitychange", this._onVisibility);
      this._stop("closed");
    }

    _build() {
      if (!this._config || !this._hass) return;
      this._built = true;
      this.shadowRoot.innerHTML = `<style>${STYLE}</style>
        <ha-card>
          <div class="head"><span class="name"></span><span class="badge" hidden></span></div>
          <div class="video"><div class="placeholder">Подключаюсь к панели…</div></div>
          <div class="controls">
            <button class="main" disabled><span class="ico">🎙</span><span class="lbl">Говорить</span></button>
            <div class="state"></div>
          </div>
          <div class="note" hidden></div>
          <div class="snap" hidden><div class="sub">Снимок последнего звонка</div><img alt=""></div>
          <div class="foot">
            <span>Это устройство: <b class="me"></b> <button class="link" data-a="rename">изменить</button></span>
            <label><input type="checkbox" class="auto"> открывать домофон при звонке</label>
          </div>
        </ha-card>`;
      const r = this.shadowRoot;
      this._btn = r.querySelector(".main");
      this._stateEl = r.querySelector(".state");
      this._note = r.querySelector(".note");
      this._btn.onclick = () => this._press();
      const auto = r.querySelector(".auto");
      auto.checked = store.get("autoswitch", "1") === "1";
      auto.onchange = () => store.set("autoswitch", auto.checked ? "1" : "0");
      r.querySelector('[data-a="rename"]').onclick = () => {
        const v = prompt("Как назвать это устройство (видно другим, кто ответил)?", label());
        if (v !== null) { store.set("label", v.trim()); this._update(); }
      };
      if (!window.isSecureContext) this._noteText("Страница открыта по http — микрофон браузер не даст. Откройте Home Assistant по https.");
      if (document.visibilityState === "visible") this._start();
      this._update();
    }

    _noteText(t) { if (this._note) { this._note.hidden = !t; this._note.textContent = t || ""; } }

    _update() {
      if (!this._built || !this._hass) return;
      const c = this._config, r = this.shadowRoot;
      const st = this._hass.states[c.entity];
      r.querySelector(".name").textContent = c.name || (st && st.attributes.friendly_name) || c.entity;
      r.querySelector(".me").textContent = label();
      const snapId = c.snapshot || (W.primary && W.primary.snapshot);
      const snap = snapId && this._hass.states[snapId];
      const box = r.querySelector(".snap");
      if (snap && snap.attributes.entity_picture) {
        const img = box.querySelector("img"), url = snap.attributes.entity_picture;
        if (img.dataset.src !== url) { img.dataset.src = url; img.src = url; }
        box.hidden = false;
      } else box.hidden = true;
    }

    _intercom(v) {
      this._ic = v;
      this._show();
      // Answered here but the microphone is off (e.g. page reloaded): end it.
      if (v && W.mine(v) && this._stream && !this._stream.microphoneTrack && !this._busy) {
        this._micDeadline = this._micDeadline || setTimeout(() => {
          this._micDeadline = null;
          if (W.mine(this._ic) && this._stream && !this._stream.microphoneTrack) W.hangup(this._ic.key);
        }, 8000);
      }
    }

    _start() {
      if (this._stream || !this._hass || !this._config) return;
      const st = this._hass.states[this._config.entity];
      const box = this.shadowRoot.querySelector(".video");
      const webrtc = customElements.get("ha-web-rtc-player");
      if (!st || (!webrtc && !customElements.get("ha-camera-stream"))) {
        box.innerHTML = `<div class="placeholder">Камера ${esc(this._config.entity)} не найдена</div>`;
        return;
      }
      const el = document.createElement(webrtc ? "ha-web-rtc-player" : "ha-camera-stream");
      el.hass = this._hass;
      if (webrtc) { el.entityid = this._config.entity; el.autoplay = true; el.playsInline = true; el.fitMode = "contain"; }
      else { el.stateObj = st; el.allowExoPlayer = true; }
      el.muted = true; el.controls = false;
      const caps = (c) => {
        this._talk = !!(c && c.two_way_audio);
        if (!this._talk) this._noteText("Разговор с этой камерой недоступен: нужен Home Assistant 2026.11+, «Настройки → Система → Labs → Двусторонняя связь» и адрес панели в настройках интеграции RaySharp.");
        this._show();
      };
      el.addEventListener("camera-capabilities-changed", (ev) => caps(ev.detail && ev.detail.capabilities));
      el.addEventListener("microphone-changed", () => this._show());
      if (webrtc) this._hass.callWS({ type: "camera/capabilities", entity_id: this._config.entity }).then(caps, () => caps(null));
      box.innerHTML = ""; box.appendChild(el);
      this._stream = el;
      this._show();
    }

    async _micOff() {
      const el = this._stream;
      try { if (el && el.microphoneTrack) await el.toggleMicrophone(); } catch (e) { /* closing anyway */ }
    }

    async _stop(why) {
      const el = this._stream;
      const mine = W.mine(this._ic);
      await this._micOff();
      if (mine) W.hangup(this._ic.key);
      if (!el) return;
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

    async _press() {
      const v = this._ic, el = this._stream;
      if (!el) return;
      this._busy = true; this._btn.disabled = true; this._noteText("");
      try {
        if (W.mine(v)) {
          await this._micOff();
          await W.hangup(v && v.key);
        } else {
          if (v) await W.answer(v.key);       // claims the call: the others go quiet
          el.muted = false;                    // hear the visitor
          if (!el.microphoneTrack) await el.toggleMicrophone();
          setTimeout(() => { if (!el.microphoneTrack && W.mine(this._ic)) W.hangup(this._ic.key); }, 6000);
        }
      } catch (e) {
        this._noteText(e && e.code === "busy" ? e.message : `Не получилось: ${e.message || e}`);
      }
      this._busy = false;
      this._show();
      setTimeout(() => this._show(), 600);
      setTimeout(() => this._show(), 1600);
    }

    _show() {
      if (!this._btn) return;
      const v = this._ic, r = this.shadowRoot;
      const mine = W.mine(v), mic = !!(this._stream && this._stream.microphoneTrack);
      const other = v && v.state === "talking" && !mine;
      const ringing = v && v.state === "ringing";
      const badge = r.querySelector(".badge");
      badge.hidden = !(ringing || v && v.state === "talking");
      badge.className = `badge ${ringing ? "ring" : "talk"}`;
      badge.textContent = ringing ? "🔔 Звонок!" : "● Разговор";
      let cls = "", ico = "🎙", lbl = "Говорить", text = "";
      if (mine || mic) { cls = "on"; ico = "⏹"; lbl = "Закончить"; text = "● Вас слышно у калитки. Пока вы говорите, микрофон панели приглушён."; }
      else if (other) {
        cls = "busy"; ico = "📵"; lbl = "Занято";
        const w = v.answered_by || {};
        text = `Разговаривает: ${w.user || "?"}${w.label ? " · " + w.label : ""}`;
      } else if (ringing) { cls = "ring"; ico = "📞"; lbl = "Ответить"; text = "Звонят в калитку"; }
      else if (this._talk) text = "Нажмите, чтобы поговорить";
      this._btn.className = `main ${cls}`;
      this._btn.querySelector(".ico").textContent = ico;
      this._btn.querySelector(".lbl").textContent = lbl;
      this._btn.disabled = !!(this._busy || !this._stream || !this._talk || !window.isSecureContext || other);
      this._stateEl.textContent = text;
    }
  }

  const STYLE = `
    ha-card { overflow: hidden; }
    .head { display:flex; align-items:center; gap: 10px; padding: 12px 16px 8px; font-size: 18px; font-weight: 500; }
    .badge { color: #fff; border-radius: 12px; padding: 2px 10px; font-size: 14px; }
    .badge.ring { background: var(--error-color, #c62828); animation: blink 1s infinite; }
    .badge.talk { background: var(--success-color, #2e7d32); }
    @keyframes blink { 50% { opacity: .55; } }
    .video { background: #000; min-height: 220px; display:flex; align-items:center; justify-content:center; }
    .video > * { width: 100%; display:block; }
    .placeholder { color: #bbb; padding: 40px 16px; text-align: center; font-size: 14px; }
    .controls { display:flex; flex-direction: column; align-items: center; gap: 8px; padding: 18px 16px 10px; }
    .main { width: 116px; height: 116px; border-radius: 50%; border: none; cursor: pointer; display:flex; flex-direction: column;
      align-items:center; justify-content:center; gap: 4px; font-size: 15px; color: #fff; background: var(--primary-color);
      box-shadow: 0 4px 14px rgba(0,0,0,.3); touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
    .main .ico { font-size: 38px; line-height: 1; }
    .main.ring { background: var(--success-color, #2e7d32); animation: pulse 1s infinite; }
    .main.on { background: var(--error-color, #c62828); }
    .main.busy, .main:disabled { background: var(--disabled-text-color, #888); cursor: default; animation: none; }
    @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(46,125,50,.7); } 70% { box-shadow: 0 0 0 20px rgba(46,125,50,0); } 100% { box-shadow: 0 0 0 0 rgba(46,125,50,0); } }
    .state { font-size: 13px; color: var(--secondary-text-color); text-align: center; min-height: 18px; }
    .note { margin: 0 16px 12px; padding: 8px 12px; border-radius: 8px; font-size: 13px;
      border-left: 4px solid var(--warning-color, #ffa600); background: var(--secondary-background-color); }
    .snap { padding: 0 16px 12px; }
    .snap img { width: 100%; max-width: 420px; border-radius: 8px; display:block; margin-top: 6px; }
    .sub { font-size: 12px; color: var(--secondary-text-color); }
    .foot { display:flex; flex-wrap: wrap; gap: 6px 16px; justify-content: space-between; padding: 8px 16px 14px;
      font-size: 12px; color: var(--secondary-text-color); border-top: 1px solid var(--divider-color); }
    .link { border: none; background: none; color: var(--primary-color); cursor: pointer; font-size: 12px; padding: 0; }
  `;

  customElements.define("raysharp-intercom-card", RaySharpIntercomCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "raysharp-intercom-card",
    name: "Домофон (RaySharp)",
    description: "Видео вызывной панели и одна кнопка: ответить, говорить, закончить.",
  });
})();
