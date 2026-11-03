// RaySharp NVR "Видео" panel. Plain web component, no build step. Talks to the
// integration over websocket (raysharp_nvr/overview, /subscribe, /event_picture,
// /action); live video through Home Assistant's own <ha-camera-stream>.

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const TYPE = {
  motion: "Движение", person: "Человек", vehicle: "Машина", line_crossing: "Линия",
  intrusion: "Вторжение", face: "Лицо", plate: "Номер", io: "Тревожный вход", doorbell: "Звонок",
  sound: "Звук", pir: "PIR", crowd: "Толпа", wander: "Блуждание", region_entrance: "Вход в зону",
  region_exiting: "Выход из зоны", occlusion: "Камеру закрыли", stationary_object: "Предмет", other: "Событие",
};
const SUFFIX = {
  motion_detected: "motion", person_detected: "person", vehicle_detected: "vehicle",
  line_crossing: "line_crossing", intrusion: "intrusion", face_detected: "face", plate_detected: "plate",
  region_entrance: "region_entrance", region_exiting: "region_exiting", wander_detected: "wander",
  stationary_object: "stationary_object", sound_detected: "sound", occlusion_detected: "occlusion",
  pir_detected: "pir", crowd_detected: "crowd",
};
// Most important first: what the badge on a tile should say.
const PRIORITY = ["doorbell", "person", "vehicle", "intrusion", "line_crossing", "face", "plate",
  "region_entrance", "region_exiting", "crowd", "wander", "stationary_object", "occlusion", "io",
  "pir", "sound", "motion", "other"];
const PTZ = [["↖", "Ptz_Cmd_LeftUp"], ["↑", "Ptz_Cmd_Up"], ["↗", "Ptz_Cmd_RightUp"],
  ["←", "Ptz_Cmd_Left"], ["", ""], ["→", "Ptz_Cmd_Right"],
  ["↙", "Ptz_Cmd_LeftDown"], ["↓", "Ptz_Cmd_Down"], ["↘", "Ptz_Cmd_RightDown"]];

const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function ago(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s} с назад`;
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  return hhmm(ts);
}

class RaySharpNvrPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._nvrs = [];
    this._events = [];
    this._pics = new Map(); // event id -> data URL
    this._tick = 0;
    this._size = "m";
    this._motionFirst = true;
    this._showEmpty = false;
    this._filter = "";
    this._open = null; // {entry, num}
    this._confirm = null;
    this._error = "";
  }

  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (first) this._start();
    else if (this._stream) this._stream.hass = h;
  }
  set narrow(v) { this._narrow = v; }
  set panel(v) {}
  get _admin() { return !!(this._hass && this._hass.user && this._hass.user.is_admin); }

  async _start() {
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><div class="title">Видео</div></div>
      <div id="body" class="body"><div class="muted">Загрузка…</div></div><div id="modal"></div>`;
    const m = this.shadowRoot.querySelector("ha-menu-button");
    if (m) { m.hass = this._hass; m.narrow = this._narrow; }
    await this._load();
    try {
      this._unsub = await this._hass.connection.subscribeMessage((msg) => {
        if (msg.event) {
          const i = this._events.findIndex((e) => e.id === msg.event.id);
          if (i >= 0) this._events[i] = msg.event; else this._events.unshift(msg.event);
          this._events = this._events.slice(0, 150);
          this._render();
        } else {
          clearTimeout(this._deb); this._deb = setTimeout(() => this._load(), 800);
        }
      }, { type: "raysharp_nvr/subscribe" });
    } catch (e) { /* polling only */ }
    // Thumbnails: the NVR serves one snapshot at a time, so refresh slowly.
    this._timer = setInterval(() => { this._tick++; this._render(); }, 15000);
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    clearInterval(this._timer);
    this._hass = undefined;
  }

  async _load() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "raysharp_nvr/overview" });
      this._nvrs = res.nvrs || [];
      this._events = res.events || [];
      this._error = "";
    } catch (e) { this._error = e.message || String(e); }
    this._render();
  }

  async _act(msg, key) {
    this._busy = key; this._render();
    try { await this._hass.callWS(msg); } catch (e) { this._error = e.message || String(e); }
    this._busy = null; await this._load();
  }

  async _picture(id) {
    if (this._pics.has(id)) return;
    this._pics.set(id, null);
    try {
      const r = await this._hass.callWS({ type: "raysharp_nvr/event_picture", event_id: id });
      if (r.image) { this._pics.set(id, `data:image/jpeg;base64,${r.image}`); this._render(true); }
    } catch (e) { /* gone from the log */ }
  }

  // Types active on a channel now: the integration's sensors plus recent events.
  _active(nvr, ch) {
    const now = Date.now() / 1000;
    const types = new Set(ch.active.map((s) => SUFFIX[s] || "motion"));
    for (const ev of this._events) {
      if (now - (ev.last || ev.ts) > nvr.event_timeout) continue;
      if (ev.channel === ch.num) types.add(ev.type);
    }
    return PRIORITY.filter((t) => types.has(t));
  }

  _render(soft = false) {
    const body = this.shadowRoot.getElementById("body");
    if (!body) return;
    if (soft && this._open) { this._renderModal(); }
    const scrollY = window.scrollY;
    body.innerHTML = (this._error ? `<div class="error">${esc(this._error)}</div>` : "")
      + (this._nvrs.length ? this._nvrs.map((n) => this._nvrView(n)).join("")
        : `<div class="muted">Нет настроенных регистраторов.</div>`);
    this._bind(body);
    window.scrollTo(0, scrollY);
    if (!soft) this._renderModal();
    // Load the first thumbnails of the feed.
    this._events.filter((e) => e.picture).slice(0, 12).forEach((e) => this._picture(e.id));
  }

  _header(n) {
    const disks = n.disks.map((d, i) => {
      const used = d.total_gb ? 100 - Math.round((d.free_gb / d.total_gb) * 100) : 0;
      const ok = !d.status || /normal|ok|норм/i.test(String(d.status));
      return `<div class="disk"><span class="sub">Диск ${i + 1}</span>
        <div class="bar"><div style="width:${used}%"></div></div>
        <span class="sub ${ok ? "" : "bad"}">${d.total_gb ? `${(d.total_gb / 1024).toFixed(1)} ТБ, свободно ${d.free_gb} ГБ` : ""} ${esc(d.status || "")}</span></div>`;
    }).join("");
    const armed = n.armed === "on";
    const ai = n.ai_today || {};
    const ask = this._confirm && this._confirm.entry === n.entry_id ? this._confirm.action : null;
    const btn = (action, label, warn) => ask === action
      ? `<span class="sub">${warn}?</span><button class="act primary" data-do="go" data-a="${action}" data-entry="${esc(n.entry_id)}">Да</button><button class="act" data-do="cancel">Нет</button>`
      : `<button class="act" data-do="ask" data-a="${action}" data-entry="${esc(n.entry_id)}">${label}</button>`;
    return `<section class="card head">
      <div class="hrow"><span class="dot ${n.online ? "ok" : "bad"}"></span><h1>${esc(n.title)}</h1>
        <span class="sub">${esc(n.model || "")} · ${esc(n.firmware || "")} · ${esc(n.ip || "")}</span></div>
      <div class="hrow wrap">
        <span class="chip ${armed ? "on" : ""}">${armed ? "🛡 Охрана включена" : "Охрана выключена"}</span>
        ${this._admin ? (armed ? btn("disarm", "Снять с охраны", "Снять с охраны") : btn("arm", "Поставить на охрану", "Поставить на охрану")) : ""}
        <span class="sub">EventPush: ${esc(n.event_push || "—")}</span>
        <span class="grow"></span>
        <span class="sub">сегодня: 🧍 ${ai.persons ?? "—"} · 🚗 ${ai.vehicles ?? "—"} · 🔢 ${ai.plates ?? "—"} · 🙂 ${ai.faces ?? "—"}</span>
      </div>
      ${(n.warnings || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("")}
      ${disks ? `<div class="disks">${disks}</div>` : ""}
      ${this._admin ? `<div class="hrow">${btn("reboot", "Перезагрузить NVR", "Перезагрузить регистратор (запись прервётся)")}</div>` : ""}
    </section>`;
  }

  _tile(n, ch) {
    const act = this._active(n, ch);
    const pic = ch.picture ? `${ch.picture}${ch.picture.includes("?") ? "&" : "?"}t=${this._tick}` : "";
    const lost = !ch.online || ch.videoloss;
    return `<button class="tile ${act.length ? "alert" : ""} ${lost ? "lost" : ""}" data-do="open" data-entry="${esc(n.entry_id)}" data-num="${ch.num}">
      <div class="img">${pic && !lost ? `<img loading="lazy" src="${esc(pic)}" alt="">` : `<div class="noimg">${lost ? "нет сигнала" : "нет кадра"}</div>`}
        ${act.length ? `<span class="badge">${esc(TYPE[act[0]] || act[0])}${act.length > 1 ? ` +${act.length - 1}` : ""}</span>` : ""}
        ${ch.recording ? '<span class="rec">● REC</span>' : ""}</div>
      <div class="cap"><b>${esc(ch.name)}</b><span class="sub">CH${ch.num}</span></div>
    </button>`;
  }

  _nvrView(n) {
    // Channels with no camera at all (empty NVR inputs) only on request.
    const used = n.channels.filter((c) => c.camera || c.online);
    const hidden = n.channels.length - used.length;
    let chans = this._showEmpty ? [...n.channels] : used;
    if (this._motionFirst) {
      chans.sort((a, b) => (this._active(n, b).length > 0) - (this._active(n, a).length > 0) || a.num - b.num);
    }
    const nameOf = Object.fromEntries(n.channels.map((c) => [c.num, c.name]));
    const types = [...new Set(this._events.map((e) => e.type))];
    const feed = this._events.filter((e) => !this._filter || e.type === this._filter).slice(0, 60).map((e) => {
      const pic = e.picture ? this._pics.get(e.id) : null;
      if (e.picture && !this._pics.has(e.id)) this._picture(e.id);
      return `<button class="ev" data-do="open" data-entry="${esc(n.entry_id)}" data-num="${e.channel}" data-ts="${e.ts}">
        ${pic ? `<img src="${pic}" alt="">` : `<div class="evicon">${esc((TYPE[e.type] || "?").slice(0, 1))}</div>`}
        <div><b>${esc(TYPE[e.type] || e.type)}</b>${e.detail ? ` <span class="plate">${esc(e.detail)}</span>` : ""}
          <div class="sub">${esc(nameOf[e.channel] || `CH${e.channel}`)} · ${ago(e.ts)}</div></div></button>`;
    }).join("");
    return `${this._header(n)}
      <div class="tools">
        <span class="sub">Размер:</span>${["s", "m", "l"].map((s) => `<button class="tab ${this._size === s ? "on" : ""}" data-do="size" data-s="${s}">${{ s: "мелко", m: "средне", l: "крупно" }[s]}</button>`).join("")}
        <label class="sub"><input type="checkbox" id="mf" ${this._motionFirst ? "checked" : ""}> с движением — вверх</label>
        ${hidden ? `<label class="sub"><input type="checkbox" id="se" ${this._showEmpty ? "checked" : ""}> пустые каналы (${hidden})</label>` : ""}
      </div>
      <div class="layout">
        <div class="grid ${this._size}">${chans.map((c) => this._tile(n, c)).join("")}</div>
        <aside class="feed card"><h2>События</h2>
          <div class="tabs"><button class="tab ${this._filter ? "" : "on"}" data-do="filter" data-t="">все</button>${types.map((t) =>
            `<button class="tab ${this._filter === t ? "on" : ""}" data-do="filter" data-t="${esc(t)}">${esc(TYPE[t] || t)}</button>`).join("")}</div>
          ${feed || '<div class="muted">Событий пока нет</div>'}</aside>
      </div>`;
  }

  _renderModal() {
    const host = this.shadowRoot.getElementById("modal");
    if (!host) return;
    if (!this._open) { host.innerHTML = ""; this._stream = null; return; }
    const n = this._nvrs.find((x) => x.entry_id === this._open.entry);
    const ch = n && n.channels.find((c) => c.num === this._open.num);
    if (!ch) { host.innerHTML = ""; return; }
    const act = this._active(n, ch);
    const evs = this._events.filter((e) => e.channel === ch.num).slice(0, 12);
    const key = `${n.entry_id}:${ch.num}:${this._open.clip || "live"}`;
    if (host.firstChild && host.dataset.key === key) {
      // Keep the live stream element; refresh only the side info.
      const info = host.querySelector("#info");
      if (info) info.innerHTML = this._modalInfo(n, ch, act, evs);
      this._bindModal(host, n, ch);
      return;
    }
    host.dataset.key = key;
    host.innerHTML = `<div class="overlay" data-do="close-bg"><div class="sheet">
      <div class="shead"><b>${esc(ch.name)}</b><span class="sub">CH${ch.num}</span>
        ${this._open.clip ? `<span class="chip">запись ${hhmm(this._open.clip)}</span><button class="act" data-do="live">● Прямой эфир</button>` : '<span class="chip onair">● эфир</span>'}
        <span class="grow"></span>
        <button class="x" data-do="close" aria-label="Закрыть">✕</button></div>
      <div id="live" class="live"></div>
      <div id="info">${this._modalInfo(n, ch, act, evs)}</div></div></div>`;
    const live = host.querySelector("#live");
    const st = ch.camera && this._hass.states[ch.camera];
    if (this._open.clip && ch.camera) {
      this._playClip(live, ch, this._open.clip);
    } else if (st && customElements.get("ha-camera-stream")) {
      const el = document.createElement("ha-camera-stream");
      el.hass = this._hass; el.stateObj = st; el.muted = true; el.controls = true; el.allowExoPlayer = true;
      live.appendChild(el); this._stream = el;
    } else if (ch.picture) {
      live.innerHTML = `<img src="${esc(ch.picture)}" alt="">`;
    } else {
      live.innerHTML = '<div class="noimg">нет видео</div>';
    }
    this._bindModal(host, n, ch);
  }

  async _playClip(box, ch, ts) {
    this._stream = null;
    box.innerHTML = '<div class="noimg">Открываю запись…</div>';
    let res;
    try {
      res = await this._hass.callWS({ type: "raysharp_nvr/clip", camera: ch.camera, ts });
    } catch (e) {
      box.innerHTML = `<div class="noimg">${esc(e.message || e)}</div>`;
      return;
    }
    if (!this._open || this._open.clip !== ts) return; // closed meanwhile
    box.innerHTML = "";
    if (customElements.get("ha-hls-player")) {
      const p = document.createElement("ha-hls-player");
      p.hass = this._hass; p.url = res.url; p.controls = true; p.muted = true; p.autoplay = true; p.playsInline = true;
      box.appendChild(p);
    } else {
      const v = document.createElement("video");
      v.src = res.url; v.controls = true; v.muted = true; v.autoplay = true; v.playsInline = true;
      box.appendChild(v);
    }
    const note = document.createElement("div");
    note.className = "sub cliphint";
    note.textContent = `Отрывок: ${res.before} с до и ${res.after} с после события. Запись с регистратора подгружается несколько секунд.`;
    box.after(note);
  }

  _modalInfo(n, ch, act, evs) {
    evs.filter((e) => e.picture).forEach((e) => this._picture(e.id));
    const ptz = this._admin && ch.ptz ? `<div class="ptz">${PTZ.map(([l, c]) => c
      ? `<button class="act" data-ptz="${c}">${l}</button>` : "<span></span>").join("")}</div>` : "";
    return `<div class="hrow wrap">
        ${act.length ? act.map((t) => `<span class="chip on">${esc(TYPE[t] || t)}</span>`).join("") : '<span class="sub">сейчас спокойно</span>'}
        ${ch.recording === true ? '<span class="chip">● запись</span>' : ch.recording === false ? '<span class="chip">запись не идёт</span>' : ""}
        <span class="grow"></span>
        ${this._admin ? `<button class="act" data-do="snap">📷 Снимок</button>` : ""}
      </div>
      ${ptz}
      <h3>События этой камеры — нажмите, чтобы посмотреть запись</h3>
      <div class="evrow">${evs.map((e) => {
        const pic = this._pics.get(e.id);
        const on = this._open.clip === e.ts;
        return `<button class="evc ${on ? "on" : ""}" data-clip="${e.ts}">${pic ? `<img src="${pic}" alt="">` : `<div class="noimg sm">▶</div>`}
          <div class="sub">▶ ${esc(TYPE[e.type] || e.type)}${e.detail ? ` ${esc(e.detail)}` : ""} · ${hhmm(e.ts)}</div></button>`;
      }).join("") || '<span class="sub">нет</span>'}</div>`;
  }

  _bindModal(host, n, ch) {
    host.querySelectorAll("[data-do]").forEach((b) => { b.onclick = (ev) => {
      if (b.dataset.do === "close-bg" && ev.target !== b) return;
      ev.stopPropagation();
      if (b.dataset.do === "close" || b.dataset.do === "close-bg") { this._open = null; this._renderModal(); }
      if (b.dataset.do === "live") { this._open = { ...this._open, clip: null }; this._renderModal(); }
      if (b.dataset.do === "snap") this._act({ type: "raysharp_nvr/action", entry_id: n.entry_id, action: "snapshot", channel: ch.num }, "snap");
    }; });
    host.querySelectorAll("[data-clip]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      this._open = { ...this._open, clip: Number(b.dataset.clip) }; this._renderModal();
      host.querySelector(".sheet")?.scrollIntoView({ block: "start" });
    }; });
    host.querySelectorAll("[data-ptz]").forEach((b) => {
      const send = (state) => this._hass.callWS({ type: "raysharp_nvr/action", entry_id: n.entry_id,
        action: "ptz", channel: ch.num, command: b.dataset.ptz, state }).catch(() => {});
      b.onpointerdown = () => send("Start");
      b.onpointerup = b.onpointerleave = () => send("Stop");
    });
  }

  _bind(root) {
    root.querySelectorAll("[data-do]").forEach((b) => b.addEventListener("click", () => {
      const d = b.dataset;
      switch (d.do) {
        case "open":
          this._open = { entry: d.entry, num: Number(d.num), clip: d.ts ? Number(d.ts) : null };
          this._renderModal(); return;
        case "size": this._size = d.s; break;
        case "filter": this._filter = d.t; break;
        case "ask": this._confirm = { entry: d.entry, action: d.a }; break;
        case "cancel": this._confirm = null; break;
        case "go": this._confirm = null;
          this._act({ type: "raysharp_nvr/action", entry_id: d.entry, action: d.a }, d.a); return;
      }
      this._render();
    }));
    const mf = root.querySelector("#mf");
    if (mf) mf.addEventListener("change", () => { this._motionFirst = mf.checked; this._render(); });
    const se = root.querySelector("#se");
    if (se) se.addEventListener("change", () => { this._showEmpty = se.checked; this._render(); });
  }
}

const STYLE = `
  :host { display:block; background: var(--primary-background-color); color: var(--primary-text-color);
    min-height: 100vh; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); overflow-x: hidden; }
  .toolbar { display:flex; align-items:center; height:56px; padding:0 12px; gap:8px;
    background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); }
  .title { font-size: 20px; }
  .body { padding: 16px; max-width: 1600px; margin: 0 auto; box-sizing: border-box; }
  h1 { font-size: 20px; font-weight: 500; margin: 0; } h2 { font-size: 15px; margin: 0 0 8px; font-weight: 600; }
  h3 { font-size: 13px; color: var(--secondary-text-color); margin: 12px 0 6px; font-weight: 500; }
  .sub { font-size: 12px; color: var(--secondary-text-color); } .muted { color: var(--secondary-text-color); padding: 8px; }
  .warn { background: color-mix(in srgb, var(--warning-color, #ffa600) 18%, transparent); border-left: 3px solid var(--warning-color, #ffa600);
    padding: 6px 10px; border-radius: 6px; font-size: 13px; }
  .bad { color: var(--error-color, #c62828); }
  .error { background: var(--error-color); color: #fff; padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; }
  .card { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px; padding: 12px 14px; box-sizing: border-box; }
  .head { margin-bottom: 10px; display:flex; flex-direction: column; gap: 8px; }
  .hrow { display:flex; align-items: center; gap: 10px; } .wrap { flex-wrap: wrap; } .grow { flex: 1; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--disabled-text-color, #999); }
  .dot.ok { background: var(--success-color, #2e7d32); } .dot.bad { background: var(--error-color, #c62828); }
  .chip { font-size: 12px; padding: 3px 10px; border-radius: 12px; background: var(--secondary-background-color); }
  .chip.on { background: var(--error-color, #c62828); color: #fff; }
  .disks { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; }
  .disk { display:flex; flex-direction: column; gap: 3px; }
  .bar { height: 8px; border-radius: 4px; background: var(--secondary-background-color); overflow: hidden; }
  .bar > div { height: 100%; background: var(--primary-color); }
  .tools { display:flex; align-items: center; gap: 6px; flex-wrap: wrap; margin: 6px 0 10px; }
  .tab { border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
    border-radius: 14px; padding: 3px 10px; cursor: pointer; font-size: 12px; }
  .tab.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .tabs { display:flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }
  .layout { display:grid; grid-template-columns: 1fr 320px; gap: 12px; align-items: start; }
  .grid { display:grid; gap: 8px; }
  .grid.s { grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
  .grid.m { grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
  .grid.l { grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); }
  .tile { padding: 0; border: 2px solid transparent; border-radius: 10px; overflow: hidden; cursor: pointer;
    background: var(--card-background-color); color: var(--primary-text-color); text-align: left; }
  .tile.alert { border-color: var(--error-color, #c62828); box-shadow: 0 0 0 3px color-mix(in srgb, var(--error-color, #c62828) 30%, transparent); }
  .tile.lost { opacity: .7; }
  .img { position: relative; aspect-ratio: 16 / 9; background: #111; }
  .img img { width: 100%; height: 100%; object-fit: cover; display:block; }
  .noimg { width: 100%; height: 100%; display:flex; align-items:center; justify-content:center; color: #bbb; font-size: 13px; aspect-ratio: 16/9; background: #222; }
  .noimg.sm { aspect-ratio: 16/9; }
  .badge { position: absolute; left: 6px; top: 6px; background: var(--error-color, #c62828); color: #fff; font-size: 12px; font-weight: 600;
    padding: 2px 8px; border-radius: 10px; animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .6; } }
  .rec { position: absolute; right: 6px; top: 6px; background: rgba(0,0,0,.55); color: #ff5252; font-size: 11px; padding: 1px 6px; border-radius: 8px; }
  .cap { display:flex; align-items: baseline; justify-content: space-between; gap: 6px; padding: 6px 8px; }
  .cap b { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .feed { position: sticky; top: 8px; max-height: calc(100vh - 90px); overflow-y: auto; }
  .ev { display:flex; gap: 8px; align-items: center; width: 100%; padding: 6px 4px; border: none; border-bottom: 1px solid var(--divider-color);
    background: transparent; color: var(--primary-text-color); text-align: left; cursor: pointer; font-size: 13px; }
  .ev img { width: 72px; height: 40px; object-fit: cover; border-radius: 4px; flex: none; }
  .evicon { width: 72px; height: 40px; border-radius: 4px; background: var(--secondary-background-color); display:flex; align-items:center; justify-content:center; flex: none; font-weight: 700; }
  .plate { font-family: monospace; background: #ffd54f; color: #000; padding: 0 4px; border-radius: 3px; }
  .act { border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color); border-radius: 8px; padding: 4px 10px; cursor: pointer; font-size: 13px; }
  .act.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6); display:flex; justify-content:center; align-items:flex-start;
    padding: calc(20px + env(safe-area-inset-top)) 10px calc(20px + env(safe-area-inset-bottom)); overflow-y:auto; z-index: 10;
    overscroll-behavior: contain; }
  .sheet { background: var(--card-background-color); border-radius: 14px; width: 100%; max-width: 980px; padding: 12px; box-sizing: border-box; }
  .shead { display:flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; font-size: 16px;
    position: sticky; top: 0; z-index: 2; background: var(--card-background-color); padding: 4px 0; }
  .x { border: none; background: var(--secondary-background-color); color: var(--primary-text-color); font-size: 20px;
    width: 40px; height: 40px; border-radius: 50%; cursor: pointer; flex: none; }
  .chip.onair { color: #ff5252; }
  .cliphint { margin-top: 6px; }
  .live video, .live ha-hls-player { width: 100%; display: block; }
  button.evc { border: 2px solid transparent; background: transparent; padding: 0; text-align: left; color: var(--primary-text-color); cursor: pointer; border-radius: 8px; }
  button.evc.on { border-color: var(--primary-color); }
  .noimg.sm { font-size: 22px; }
  .live { background: #000; border-radius: 8px; overflow: hidden; min-height: 200px; }
  .live img, .live ha-camera-stream { width: 100%; display:block; }
  .ptz { display:grid; grid-template-columns: repeat(3, 44px); gap: 4px; margin-top: 10px; }
  .ptz .act { padding: 6px 0; font-size: 16px; }
  .evrow { display:grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
  .evc img { width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 6px; display:block; }
  @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } .feed { position: static; max-height: none; } }
  @media (max-width: 600px) {
    .overlay { padding: 0; background: var(--card-background-color); }
    .sheet { border-radius: 0; min-height: 100%; padding: calc(8px + env(safe-area-inset-top)) 10px calc(16px + env(safe-area-inset-bottom)); }
    .shead { top: calc(-8px - env(safe-area-inset-top)); padding-top: calc(8px + env(safe-area-inset-top)); margin-top: calc(-8px - env(safe-area-inset-top)); }
    .body { padding: 8px; } .grid.m, .grid.l { grid-template-columns: 1fr; } .grid.s { grid-template-columns: repeat(2, minmax(0,1fr)); } }
`;

if (!customElements.get("raysharp-nvr-panel")) customElements.define("raysharp-nvr-panel", RaySharpNvrPanel);
