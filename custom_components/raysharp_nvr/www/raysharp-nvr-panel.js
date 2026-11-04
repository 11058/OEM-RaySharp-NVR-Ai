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
  smart: "AI-обнаружение",
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
  "pir", "sound", "smart", "motion", "other"];
const PTZ = [["↖", "Ptz_Cmd_LeftUp"], ["↑", "Ptz_Cmd_Up"], ["↗", "Ptz_Cmd_RightUp"],
  ["←", "Ptz_Cmd_Left"], ["", ""], ["→", "Ptz_Cmd_Right"],
  ["↙", "Ptz_Cmd_LeftDown"], ["↓", "Ptz_Cmd_Down"], ["↘", "Ptz_Cmd_RightDown"]];

const PERIODS = [[3600, "1 ч"], [6 * 3600, "6 ч"], [86400, "сутки"], [3 * 86400, "3 дня"], [7 * 86400, "неделя"]];
function loadPeriod() {
  try { const v = Number(localStorage.getItem("raysharp_nvr_period")); if (PERIODS.some(([p]) => p === v)) return v; } catch (e) {}
  return 86400;
}
function hhmm(ts) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  if (d.toDateString() === new Date().toDateString()) return t;
  return `${d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })} ${t.slice(0, 5)}`;
}
function ago(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s} с назад`;
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  return hhmm(ts);
}

const PROFILES = [["auto", "Авто"], ["main", "Основной"], ["sub", "Доп."], ["mobile", "Моб."]];
const store = {
  get(k, d) { try { const v = localStorage.getItem(`raysharp_nvr_${k}`); return v === null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(`raysharp_nvr_${k}`, String(v)); } catch (e) { /* private mode */ } },
};

// ha-hls-player ships with HA's camera UI, which loads lazily; pull it in once.
let playerReady;
function ensurePlayer() {
  if (customElements.get("ha-hls-player")) return Promise.resolve(true);
  if (!playerReady) {
    playerReady = (async () => {
      try {
        const helpers = window.loadCardHelpers ? await window.loadCardHelpers() : null;
        if (helpers) helpers.createCardElement({ type: "picture-entity", entity: "camera.x", camera_view: "live" });
      } catch (e) { /* fall back to <video> */ }
      await Promise.race([customElements.whenDefined("ha-hls-player"), new Promise((r) => setTimeout(r, 6000))]);
      return !!customElements.get("ha-hls-player");
    })();
  }
  return playerReady;
}

async function makePlayer(hass, url, { controls = false } = {}) {
  // ha-hls-player resolves the variant playlist against its url, so it must
  // be absolute: a bare /api/hls/... path throws "Invalid base URL".
  url = hass.hassUrl ? hass.hassUrl(url) : new URL(url, location.origin).href;
  if (await ensurePlayer()) {
    const p = document.createElement("ha-hls-player");
    p.hass = hass; p.url = url; p.controls = controls; p.muted = true;
    p.autoPlay = true; p.autoplay = true; p.playsInline = true; p.allowExoPlayer = true;
    return p;
  }
  const v = document.createElement("video");
  v.src = url; v.controls = controls; v.muted = true; v.autoplay = true; v.playsInline = true;
  return v;
}

class RaySharpNvrPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._nvrs = [];
    this._events = [];
    this._pics = new Map(); // event id -> data URL
    this._tiles = new Map(); // "entry:num" -> {el, img, media, ovl, cap, live}
    this._tick = 0;
    this._gen = 0;
    this._size = store.get("size", "m");
    this._motionFirst = store.get("motion_first", "1") === "1";
    this._liveGrid = store.get("live_grid", "1") === "1";
    this._profile = store.get("profile", window.innerWidth < 700 ? "sub" : "auto");
    this._showEmpty = false;
    this._period = loadPeriod();
    this._filter = "";
    this._open = null; // {entry, num, clip}
    this._confirm = null;
    this._error = "";
  }

  // HA keeps the panel element and re-attaches it when the user comes back
  // from another page: everything started in _start must be undone on
  // detach and started again on attach, or the page works only until the
  // first navigation.
  set hass(h) {
    this._hass = h;
    if (!this._started && this.isConnected) this._start();
    else if (this._stream) this._stream.hass = h;
  }
  connectedCallback() {
    if (this._hass && !this._started) this._start();
  }
  set narrow(v) { this._narrow = v; }
  set panel(v) {}
  get _admin() { return !!(this._hass && this._hass.user && this._hass.user.is_admin); }

  async _start() {
    this._started = true;
    const gen = ++this._gen;
    this._tiles.clear(); this._stream = null;
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><div class="title">Видео</div></div>
      <div id="body" class="body"><div class="muted">Загрузка…</div></div><div id="modal"></div>`;
    const m = this.shadowRoot.querySelector("ha-menu-button");
    if (m) { m.hass = this._hass; m.narrow = this._narrow; }
    const body = this.shadowRoot.getElementById("body");
    body.addEventListener("click", (ev) => this._onClick(ev));
    body.addEventListener("change", (ev) => this._onChange(ev));
    await this._load();
    if (gen !== this._gen) return;
    try {
      const unsub = await this._hass.connection.subscribeMessage((msg) => {
        if (msg.event) {
          const i = this._events.findIndex((e) => e.id === msg.event.id);
          if (i >= 0) this._events[i] = msg.event; else this._events.unshift(msg.event);
          this._events.sort((a, b) => b.ts - a.ts);
          const cut = Date.now() / 1000 - this._period;
          this._events = this._events.filter((e) => e.ts >= cut).slice(0, 1000);
          this._render();
        } else {
          clearTimeout(this._deb); this._deb = setTimeout(() => this._load(), 800);
        }
      }, { type: "raysharp_nvr/subscribe" });
      if (gen !== this._gen) unsub(); else this._unsub = unsub;
    } catch (e) { /* polling only */ }
    if (gen !== this._gen) return;
    // Snapshot tiles (when live video is off or fails) refresh slowly: the
    // NVR serves one snapshot at a time.
    this._timer = setInterval(() => { this._tick++; this._render(); }, 15000);
  }

  disconnectedCallback() {
    this._gen++; this._started = false;
    if (this._unsub) { try { this._unsub(); } catch (e) { /* socket gone */ } }
    this._unsub = null;
    clearInterval(this._timer); clearTimeout(this._deb);
    for (const t of this._tiles.values()) clearTimeout(t.watch);
    this._tiles.clear(); this._stream = null;
    if (this.shadowRoot) this.shadowRoot.innerHTML = "";
  }

  async _load() {
    if (!this._hass || !this._started) return;
    try {
      const res = await this._hass.callWS({
        type: "raysharp_nvr/overview", since: Date.now() / 1000 - this._period, limit: 1000,
      });
      this._nvrs = res.nvrs || [];
      this._events = res.events || [];
      this._lastEvent = res.last_event || null;
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
      if (ev.history || now - (ev.last || ev.ts) > nvr.event_timeout) continue;
      if (ev.channel === ch.num) types.add(ev.type);
    }
    return PRIORITY.filter((t) => types.has(t));
  }

  // ── page ──────────────────────────────────────────────────────────────
  // The page is built once per NVR and patched afterwards: tiles carry live
  // video that must not be torn down on every update.
  _render(soft = false) {
    if (!this._started || !this._hass) return;
    const body = this.shadowRoot.getElementById("body");
    if (!body) return;
    if (!this._nvrs.length) {
      body.innerHTML = (this._error ? `<div class="error">${esc(this._error)}</div>` : "")
        + '<div class="muted">Нет настроенных регистраторов.</div>';
      this._tiles.clear();
      return;
    }
    if (body.querySelector(".muted") && !body.querySelector(".nvr")) body.innerHTML = "";
    let err = body.querySelector(":scope > .error");
    if (this._error) {
      if (!err) { err = document.createElement("div"); err.className = "error"; body.prepend(err); }
      err.textContent = this._error;
    } else if (err) err.remove();

    const seen = new Set();
    for (const n of this._nvrs) {
      seen.add(n.entry_id);
      let sec = body.querySelector(`.nvr[data-entry="${CSS.escape(n.entry_id)}"]`);
      if (!sec) {
        sec = document.createElement("div");
        sec.className = "nvr"; sec.dataset.entry = n.entry_id;
        sec.innerHTML = `<div class="h"></div><div class="tools"></div>
          <div class="layout"><div class="grid"></div><aside class="feed card"></aside></div>`;
        body.appendChild(sec);
      }
      sec.querySelector(".h").innerHTML = this._header(n);
      sec.querySelector(".tools").innerHTML = this._toolsView(n);
      this._grid(n, sec.querySelector(".grid"));
      if (!soft || true) sec.querySelector(".feed").innerHTML = this._feedView(n);
    }
    body.querySelectorAll(".nvr").forEach((s) => { if (!seen.has(s.dataset.entry)) s.remove(); });
    if (!soft) this._renderModal(); else if (this._open) this._renderModal();
    this._events.filter((e) => e.picture).slice(0, 12).forEach((e) => this._picture(e.id));
  }

  _header(n) {
    const disks = n.disks.map((d, i) => {
      const used = d.total_gb ? 100 - Math.round((d.free_gb / d.total_gb) * 100) : 0;
      const st = String(d.status || "");
      // Full / Overwrite is the normal state of a recorder: it writes over
      // the oldest footage. Only other states are a problem.
      const cycling = /overwrite|full/i.test(st);
      const ok = !st || cycling || /normal|ok|норм/i.test(st);
      const label = cycling ? "заполнен, запись идёт поверх старой" : st;
      return `<div class="disk"><span class="sub">Диск ${i + 1}</span>
        <div class="bar"><div style="width:${used}%"></div></div>
        <span class="sub ${ok ? "" : "bad"}">${d.total_gb ? `${(d.total_gb / 1024).toFixed(1)} ТБ${cycling ? "" : `, свободно ${d.free_gb} ГБ`}` : ""} · ${esc(label)}</span></div>`;
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

  _toolsView(n) {
    const hidden = n.channels.filter((c) => !(c.camera || c.online)).length;
    return `<span class="sub">Размер:</span>${["s", "m", "l"].map((s) => `<button class="tab ${this._size === s ? "on" : ""}" data-do="size" data-s="${s}">${{ s: "мелко", m: "средне", l: "крупно" }[s]}</button>`).join("")}
      <label class="sub"><input type="checkbox" data-opt="live" ${this._liveGrid ? "checked" : ""}> живое видео (лёгкий поток)</label>
      <label class="sub"><input type="checkbox" data-opt="mf" ${this._motionFirst ? "checked" : ""}> с движением — вверх</label>
      ${hidden ? `<label class="sub"><input type="checkbox" data-opt="se" ${this._showEmpty ? "checked" : ""}> пустые каналы (${hidden})</label>` : ""}`;
  }

  _grid(n, grid) {
    grid.className = `grid ${this._size}`;
    const chans = n.channels.filter((c) => this._showEmpty || c.camera || c.online);
    const order = [...chans];
    if (this._motionFirst) {
      order.sort((a, b) => (this._active(n, b).length > 0) - (this._active(n, a).length > 0) || a.num - b.num);
    }
    const keep = new Set();
    order.forEach((ch, idx) => {
      const key = `${n.entry_id}:${ch.num}`;
      keep.add(key);
      let t = this._tiles.get(key);
      if (!t || !t.el.isConnected || t.el.parentNode !== grid) {
        const el = document.createElement("div");
        el.className = "tile"; el.dataset.do = "open"; el.dataset.entry = n.entry_id; el.dataset.num = ch.num;
        el.innerHTML = `<div class="img"><div class="media"></div><div class="ovl"></div></div><div class="cap"></div>`;
        grid.appendChild(el);
        t = { el, media: el.querySelector(".media"), ovl: el.querySelector(".ovl"), cap: el.querySelector(".cap"), mode: "" };
        this._tiles.set(key, t);
      }
      // CSS order instead of moving nodes: moving would restart the video.
      t.el.style.order = String(idx);
      const act = this._active(n, ch);
      const lost = !ch.online || ch.videoloss;
      t.el.className = `tile ${act.length ? "alert" : ""} ${lost ? "lost" : ""}`;
      t.ovl.innerHTML = `${act.length ? `<span class="badge">${esc(TYPE[act[0]] || act[0])}${act.length > 1 ? ` +${act.length - 1}` : ""}</span>` : ""}
        ${ch.recording ? '<span class="rec">● REC</span>' : ""}`;
      t.cap.innerHTML = `<b>${esc(ch.name)}</b><span class="sub">CH${ch.num}</span>`;
      this._tileMedia(t, ch, lost);
    });
    for (const [key, t] of this._tiles) {
      if (key.startsWith(`${n.entry_id}:`) && !keep.has(key)) { t.el.remove(); this._tiles.delete(key); }
    }
  }

  _tileMedia(t, ch, lost) {
    if (lost || !ch.camera) {
      if (t.mode !== "lost") { t.media.innerHTML = `<div class="noimg">${lost ? "нет сигнала" : "нет кадра"}</div>`; t.mode = "lost"; }
      return;
    }
    if (this._liveGrid && !t.liveFailed) {
      if (t.mode === "live" || t.mode === "starting") return;
      t.mode = "starting";
      const gen = this._gen;
      this._hass.callWS({ type: "raysharp_nvr/stream", camera: ch.camera, profile: "lowest" })
        .then(async (res) => {
          if (t.mode !== "starting" || gen !== this._gen) return;
          const p = await makePlayer(this._hass, res.url);
          if (t.mode !== "starting" || gen !== this._gen) return;
          p.classList.add("tilevid");
          t.media.innerHTML = ""; t.media.appendChild(p); t.mode = "live";
          this._watchTile(t, ch, p, gen);
        })
        .catch(() => {
          if (gen !== this._gen) return;
          t.liveFailed = true; t.mode = ""; this._tileMedia(t, ch, lost);
        });
      if (!t.media.firstChild && ch.picture) t.media.innerHTML = `<img src="${esc(ch.picture)}" alt="">`;
      return;
    }
    const pic = ch.picture ? `${ch.picture}${ch.picture.includes("?") ? "&" : "?"}t=${this._tick}` : "";
    if (t.mode === "pic" && t.pic === pic) return;
    t.media.innerHTML = pic ? `<img loading="lazy" src="${esc(pic)}" alt="">` : '<div class="noimg">нет кадра</div>';
    t.mode = "pic"; t.pic = pic;
  }

  // A stream that never starts leaves ha-hls-player showing its error
  // banner forever. Ask once more (the server drops a dead stream and moves
  // to the next profile), then settle for refreshed snapshots.
  _watchTile(t, ch, p, gen) {
    clearTimeout(t.watch);
    t.watch = setTimeout(() => {
      if (gen !== this._gen || t.mode !== "live" || !p.isConnected) return;
      const sr = p.shadowRoot;
      const failed = sr && sr.querySelector("ha-alert");
      if (!failed) return;
      t.mode = "";
      if (t.retried) t.liveFailed = true; else t.retried = true;
      t.media.innerHTML = "";
      this._tileMedia(t, ch, false);
    }, 25000);
  }

  _feedView(n) {
    const nameOf = Object.fromEntries(n.channels.map((c) => [c.num, c.name]));
    const types = [...new Set(this._events.map((e) => e.type))];
    const list = this._events.filter((e) => !this._filter || e.type === this._filter);
    const feed = list.slice(0, this._feedLimit || 100).map((e) => {
      const pic = e.picture ? this._pics.get(e.id) : null;
      if (e.picture && !this._pics.has(e.id)) this._picture(e.id);
      return `<button class="ev" data-do="open" data-entry="${esc(n.entry_id)}" data-num="${e.channel}" data-ts="${e.ts}" data-end="${e.rec_end || ""}">
        ${pic ? `<img src="${pic}" alt="">` : `<div class="evicon">${esc((TYPE[e.type] || "?").slice(0, 1))}</div>`}
        <div><b>${esc(TYPE[e.type] || e.type)}</b>${e.detail ? ` <span class="plate">${esc(e.detail)}</span>` : ""}
          <div class="sub">${esc(nameOf[e.channel] || `CH${e.channel}`)} · ${ago(e.ts)}</div></div></button>`;
    }).join("");
    return `<div class="hrow"><h2>События</h2><span class="grow"></span>
        <select data-opt="period" class="sel">${PERIODS.map(([p, l]) => `<option value="${p}" ${p === this._period ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div class="tabs"><button class="tab ${this._filter ? "" : "on"}" data-do="filter" data-t="">все</button>${types.map((t) =>
        `<button class="tab ${this._filter === t ? "on" : ""}" data-do="filter" data-t="${esc(t)}">${esc(TYPE[t] || t)}</button>`).join("")}</div>
      ${feed || `<div class="muted">За этот период событий нет${this._lastEvent
        ? `. Последнее — ${hhmm(this._lastEvent)}${this._period < 7 * 86400 ? ' <button class="act" data-do="week">показать за неделю</button>' : ""}` : ""}</div>`}
      ${list.length > (this._feedLimit || 100) ? '<button class="act more" data-do="more">Показать ещё</button>' : ""}`;
  }

  _onClick(ev) {
    const b = ev.target.closest("[data-do]");
    if (!b) return;
    const d = b.dataset;
    switch (d.do) {
      case "open":
        this._open = { entry: d.entry, num: Number(d.num), clip: d.ts ? Number(d.ts) : null,
          until: d.end ? Number(d.end) : null };
        this._renderModal(); return;
      case "size": this._size = d.s; store.set("size", d.s); break;
      case "filter": this._filter = d.t; this._feedLimit = 100; break;
      case "more": this._feedLimit = (this._feedLimit || 100) + 100; break;
      case "week": this._period = 7 * 86400; store.set("period", this._period); this._load(); return;
      case "ask": this._confirm = { entry: d.entry, action: d.a }; break;
      case "cancel": this._confirm = null; break;
      case "go": this._confirm = null;
        this._act({ type: "raysharp_nvr/action", entry_id: d.entry, action: d.a }, d.a); return;
      default: return;
    }
    this._render();
  }

  _onChange(ev) {
    const el = ev.target;
    switch (el.dataset.opt) {
      case "mf": this._motionFirst = el.checked; store.set("motion_first", el.checked ? 1 : 0); break;
      case "se": this._showEmpty = el.checked; break;
      case "live":
        this._liveGrid = el.checked; store.set("live_grid", el.checked ? 1 : 0);
        for (const t of this._tiles.values()) { clearTimeout(t.watch); t.mode = ""; t.liveFailed = false; t.retried = false; t.media.innerHTML = ""; }
        break;
      case "period":
        this._period = Number(el.value); this._feedLimit = 100;
        store.set("period", this._period);
        this._load(); return;
      default: return;
    }
    this._render();
  }

  // ── camera window ─────────────────────────────────────────────────────
  _renderModal() {
    const host = this.shadowRoot.getElementById("modal");
    if (!host) return;
    if (!this._open) { host.innerHTML = ""; host.dataset.key = ""; this._stream = null; return; }
    const n = this._nvrs.find((x) => x.entry_id === this._open.entry);
    const ch = n && n.channels.find((c) => c.num === this._open.num);
    if (!ch) { host.innerHTML = ""; return; }
    const act = this._active(n, ch);
    const evs = this._events.filter((e) => e.channel === ch.num).slice(0, 12);
    const profiles = PROFILES.filter(([p]) => p === "auto" || (ch.streams || []).includes(p));
    const profile = profiles.some(([p]) => p === this._profile) ? this._profile : "auto";
    const key = `${n.entry_id}:${ch.num}:${this._open.clip || "live"}:${profile}`;
    if (host.firstChild && host.dataset.key === key) {
      // Keep the playing video; refresh only the info below it.
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
      ${this._open.clip ? "" : `<div class="tabs prof">${profiles.map(([p, l]) =>
        `<button class="tab ${p === profile ? "on" : ""}" data-prof="${p}">${l}</button>`).join("")}</div>`}
      <div id="live" class="live"></div>
      <div id="info">${this._modalInfo(n, ch, act, evs)}</div></div></div>`;
    const live = host.querySelector("#live");
    const st = ch.camera && this._hass.states[ch.camera];
    this._stream = null;
    const hlsLive = () => {
      if (profile !== "auto") return this._playProfile(live, ch, profile, key);
      if (st && customElements.get("ha-camera-stream")) {
        const el = document.createElement("ha-camera-stream");
        el.hass = this._hass; el.stateObj = st; el.muted = false; el.controls = true; el.allowExoPlayer = true;
        live.innerHTML = ""; live.appendChild(el); this._stream = el;
      }
    };
    if (this._open.clip && ch.camera) {
      const { clip, until } = this._open;
      this._playAv(live, key, { camera: ch.camera, ts: clip, ...(until ? { until } : {}) },
        () => this._playClip(live, ch, clip, until), "Открываю запись…");
    } else if (ch.camera) {
      this._playAv(live, key, { camera: ch.camera, profile: profile === "auto" ? "main" : profile },
        hlsLive, "Подключаюсь…");
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

  // MP4 from the integration with the camera's sound re-encoded to AAC:
  // HA's HLS drops the G.711 audio these cameras send. Falls back to HLS
  // (silent) when ffmpeg cannot open the source or the browser cannot play it.
  async _playAv(box, key, msg, fallback, wait) {
    const host = () => this.shadowRoot && this.shadowRoot.getElementById("modal");
    const current = () => box.isConnected && host() && host().dataset.key === key;
    box.innerHTML = `<div class="noimg">${wait}</div>`;
    let res;
    try {
      res = await this._hass.callWS({ type: "raysharp_nvr/av", ...msg });
    } catch (e) {
      if (!current()) return;
      if (e && e.code === "no_recording") { box.innerHTML = `<div class="noimg">${esc(e.message)}</div>`; return; }
      fallback(); this._avNote(box, `🔇 видео без звука: ${e.message || e.code || e}`); return;
    }
    if (!current()) return;
    const v = document.createElement("video");
    v.controls = true; v.autoplay = true; v.playsInline = true; v.muted = false;
    let done = false;
    v.addEventListener("error", () => {
      if (done || !current()) return;
      done = true;
      box.parentNode.querySelectorAll(".cliphint").forEach((n) => n.remove());
      const why = v.error ? `код ${v.error.code}${v.error.message ? `, ${v.error.message}` : ""}` : "";
      fallback();
      this._avNote(box, `🔇 видео без звука: браузер не открыл поток со звуком (${why})`);
    }, { once: true });
    v.addEventListener("playing", () => {
      done = true;
      const a = res.audio;
      const name = { pcm_alaw: "G.711A", pcm_mulaw: "G.711U", adpcm_g726: "G.726", aac: "AAC" }[a] || a;
      this._avNote(box, a === "" ? "🔇 камера не передаёт звук — включите аудио в настройках канала на регистраторе"
        : a === "?" ? "🔊 звук: формат не определился"
        : `🔊 звук: ${name} → AAC${v.muted ? " — включён без звука, нажмите на громкость" : ""}`);
      setTimeout(() => {
        // Chrome counts decoded audio; nothing after a few seconds = silent track.
        if (current() && v.webkitAudioDecodedByteCount === 0 && a) this._avNote(box, `🔇 звуковая дорожка ${name} есть, но пустая`);
      }, 6000);
    }, { once: true });
    v.src = this._hass.hassUrl ? this._hass.hassUrl(res.url) : res.url;
    box.innerHTML = ""; box.appendChild(v);
    if (res.before !== undefined) {
      const note = document.createElement("div");
      note.className = "sub cliphint";
      note.textContent = `Отрывок: ${res.before} с до и ${res.after} с после события. Запись с регистратора подгружается несколько секунд.`;
      box.after(note);
    }
    try { await v.play(); } catch (e) {
      // Autoplay with sound refused: play muted, the controls unmute it.
      if (e && e.name === "NotAllowedError") { v.muted = true; v.play().catch(() => {}); }
    }
  }

  _avNote(box, text) {
    if (!box.isConnected) return;
    let n = box.parentNode.querySelector(".avnote");
    if (!n) { n = document.createElement("div"); n.className = "sub avnote"; box.after(n); }
    n.textContent = text;
  }

  async _playProfile(box, ch, profile, key) {
    box.innerHTML = '<div class="noimg">Подключаюсь…</div>';
    try {
      const res = await this._hass.callWS({ type: "raysharp_nvr/stream", camera: ch.camera, profile });
      const host = this.shadowRoot.getElementById("modal");
      if (!box.isConnected || !host || host.dataset.key !== key) return;
      const p = await makePlayer(this._hass, res.url, { controls: true });
      if (!box.isConnected) return;
      box.innerHTML = ""; box.appendChild(p);
    } catch (e) {
      box.innerHTML = `<div class="noimg">${esc(e.message || e)}</div>`;
    }
  }

  async _playClip(box, ch, ts, until) {
    box.innerHTML = '<div class="noimg">Открываю запись…</div>';
    let res;
    try {
      res = await this._hass.callWS({ type: "raysharp_nvr/clip", camera: ch.camera, ts, ...(until ? { until } : {}) });
    } catch (e) {
      box.innerHTML = `<div class="noimg">${esc(e.message || e)}</div>`;
      return;
    }
    if (!box.isConnected || !this._open || this._open.clip !== ts) return; // closed meanwhile
    const p = await makePlayer(this._hass, res.url, { controls: true });
    if (!box.isConnected) return;
    box.innerHTML = ""; box.appendChild(p);
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
        return `<button class="evc ${on ? "on" : ""}" data-clip="${e.ts}" data-end="${e.rec_end || ""}">${pic ? `<img src="${pic}" alt="">` : `<div class="noimg sm">▶</div>`}
          <div class="sub">▶ ${esc(TYPE[e.type] || e.type)}${e.detail ? ` ${esc(e.detail)}` : ""} · ${hhmm(e.ts)}</div></button>`;
      }).join("") || '<span class="sub">нет</span>'}</div>`;
  }

  _bindModal(host, n, ch) {
    host.querySelectorAll("[data-do]").forEach((b) => { b.onclick = (ev) => {
      if (b.dataset.do === "close-bg" && ev.target !== b) return;
      ev.stopPropagation();
      if (b.dataset.do === "close" || b.dataset.do === "close-bg") { this._open = null; this._renderModal(); }
      if (b.dataset.do === "live") { this._open = { ...this._open, clip: null, until: null }; this._renderModal(); }
      if (b.dataset.do === "snap") this._act({ type: "raysharp_nvr/action", entry_id: n.entry_id, action: "snapshot", channel: ch.num }, "snap");
    }; });
    host.querySelectorAll("[data-prof]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      this._profile = b.dataset.prof; store.set("profile", this._profile); this._renderModal();
    }; });
    host.querySelectorAll("[data-clip]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      this._open = { ...this._open, clip: Number(b.dataset.clip), until: b.dataset.end ? Number(b.dataset.end) : null };
      this._renderModal();
      host.querySelector(".sheet")?.scrollIntoView({ block: "start" });
    }; });
    host.querySelectorAll("[data-ptz]").forEach((b) => {
      const send = (state) => this._hass.callWS({ type: "raysharp_nvr/action", entry_id: n.entry_id,
        action: "ptz", channel: ch.num, command: b.dataset.ptz, state }).catch(() => {});
      b.onpointerdown = () => send("Start");
      b.onpointerup = b.onpointerleave = () => send("Stop");
    });
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
  .sel { background: var(--card-background-color); color: var(--primary-text-color); border: 1px solid var(--divider-color);
    border-radius: 8px; padding: 3px 6px; font-size: 13px; }
  .more { width: 100%; margin-top: 8px; }
  .feed h2 { margin: 0; }
  .feed .hrow { margin-bottom: 8px; }
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
  .media { position: absolute; inset: 0; overflow: hidden; }
  .media .tilevid, .media video { width: 100%; height: 100%; display: block; object-fit: cover; pointer-events: none;
    --video-max-height: 100%; }
  .ovl { position: absolute; inset: 0; pointer-events: none; }
  .tile { display: block; }
  .prof { margin-bottom: 8px; }
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
