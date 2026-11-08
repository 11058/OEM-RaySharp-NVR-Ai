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
const PTZ = [["↖", "Ptz_Cmd_UpLeft"], ["↑", "Ptz_Cmd_Up"], ["↗", "Ptz_Cmd_UpRight"],
  ["←", "Ptz_Cmd_Left"], ["", ""], ["→", "Ptz_Cmd_Right"],
  ["↙", "Ptz_Cmd_DownLeft"], ["↓", "Ptz_Cmd_Down"], ["↘", "Ptz_Cmd_DownRight"]];

const PERIODS = [[3600, "1 ч"], [6 * 3600, "6 ч"], [86400, "сутки"], [3 * 86400, "3 дня"], [7 * 86400, "неделя"]];
function loadPeriod() {
  try { const v = Number(localStorage.getItem("raysharp_nvr_period")); if (PERIODS.some(([p]) => p === v)) return v; } catch (e) {}
  return 86400;
}
// The NVR reports binary GB (GiB). Shown in one unit, rounded the way disks are
// sold: an "8 TB" disk is 7452 GiB = 7.3 TiB; free space in the same unit.
function size(gib) {
  if (gib === null || gib === undefined) return "?";
  const n = Number(gib);
  if (n >= 1024) return `${(n / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} ТБ`;
  return `${Math.round(n).toLocaleString("ru-RU")} ГБ`;
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
// Recordings: NVRs usually record only the main stream; the others are offered
// when the camera has them and the NVR says why when it does not record them.
const REC_PROFILES = [["main", "Основной"], ["sub", "Доп."], ["mobile", "Моб."]];
const SEG = { normal: "запись", motion: "движение", smart: "AI", pir: "PIR", io: "тревога" };
const SPANS = [[86400, "24 ч"], [6 * 3600, "6 ч"], [3600, "1 ч"], [600, "10 мин"]];
const STALL_S = 20; // no picture after this long = tell the user the stream did not connect
function dayStr(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function hm(ts, sec = false) {
  return new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", ...(sec ? { second: "2-digit" } : {}) });
}
const store = {
  get(k, d) { try { const v = localStorage.getItem(`raysharp_nvr_${k}`); return v === null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(`raysharp_nvr_${k}`, String(v)); } catch (e) { /* private mode */ } },
};

// Safari and every iOS browser / the HA app (WebKit): no endless progressive
// MP4, but native HLS. They get the integration's HLS-with-sound instead.
const APPLE = (() => {
  const ua = navigator.userAgent || "";
  return /AppleWebKit/.test(ua) && !/Chrome|Chromium|CriOS|Edg|OPR|Android/.test(ua)
    || /iPad|iPhone|iPod/.test(ua);
})();

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
    this._open = null; // {entry, num, clip, until, mode: live | clip | archive}
    this._recProfile = store.get("rec_profile", "main");
    this._arch = null; // archive: {cam, day, data, span, center, play, chunk, video}
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
      this._journal = res.journal || null;
      this._error = "";
    } catch (e) { this._error = e.message || String(e); }
    this._deepLink();
    this._render();
  }

  // /video?camera=camera.ch1_cam01&mode=talk opens that camera straight away
  // (live | talk | archive): the "Answer" button of a doorbell notification.
  _deepLink() {
    let q;
    try { q = new URLSearchParams(location.search); } catch (e) { return; }
    const cam = q.get("camera");
    if (!cam || this._linked === location.search) return;
    for (const n of this._nvrs) {
      const ch = n.channels.find((c) => c.camera === cam);
      if (!ch) continue;
      this._linked = location.search;
      const mode = ["live", "talk", "archive"].includes(q.get("mode")) ? q.get("mode") : "live";
      this._open = { entry: n.entry_id, num: ch.num, clip: null, until: null, mode: mode === "talk" && !ch.talk ? "live" : mode };
      this._renderModal();
      return;
    }
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
        <span class="sub ${ok ? "" : "bad"}">${d.total_gb ? `${size(d.total_gb)}${cycling ? "" : `, свободно ${size(d.free_gb)}`}` : ""} · ${esc(label)}</span></div>`;
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
      // No recording left (archive overwritten, disk formatted): open the camera, not a clip.
      const ts = e.gone ? "" : e.ts;
      return `<button class="ev ${e.gone ? "gone" : ""}" data-do="open" data-entry="${esc(n.entry_id)}" data-num="${e.channel}" data-ts="${ts}" data-end="${e.gone ? "" : e.rec_end || ""}">
        ${pic ? `<img src="${pic}" alt="">` : `<div class="evicon">${esc((TYPE[e.type] || "?").slice(0, 1))}</div>`}
        <div><b>${esc(TYPE[e.type] || e.type)}</b>${e.detail ? ` <span class="plate">${esc(e.detail)}</span>` : ""}
          <div class="sub">${esc(nameOf[e.channel] || `CH${e.channel}`)} · ${ago(e.ts)}${e.gone ? ' · <span class="warn">запись удалена</span>' : ""}</div></div></button>`;
    }).join("");
    return `<div class="hrow"><h2>События</h2><span class="grow"></span>
        <select data-opt="period" class="sel">${PERIODS.map(([p, l]) => `<option value="${p}" ${p === this._period ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div class="tabs"><button class="tab ${this._filter ? "" : "on"}" data-do="filter" data-t="">все</button>${types.map((t) =>
        `<button class="tab ${this._filter === t ? "on" : ""}" data-do="filter" data-t="${esc(t)}">${esc(TYPE[t] || t)}</button>`).join("")}</div>
      ${feed || `<div class="muted">За этот период событий нет${this._lastEvent
        ? `. Последнее — ${hhmm(this._lastEvent)}${this._period < 7 * 86400 ? ' <button class="act" data-do="week">показать за неделю</button>' : ""}` : ""}</div>`}
      ${list.length > (this._feedLimit || 100) ? '<button class="act more" data-do="more">Показать ещё</button>' : ""}
      ${this._journalBar()}`;
  }

  // Journal size, limits and clearing (admin).
  _journalBar() {
    const j = this._journal;
    if (!j) return "";
    const ask = this._confirm && this._confirm.journal;
    const what = ask === "all" ? `все ${j.events} событий и ${j.pictures} снимков` : `${j.gone} событий без записи`;
    const btns = !this._admin ? "" : ask
      ? `<span class="sub">Удалить ${what}?</span>
         <button class="act primary" data-do="jclear" data-m="${ask}">Да</button><button class="act" data-do="cancel">Нет</button>`
      : `${j.gone ? `<button class="act" data-do="jask" data-m="gone">Убрать события без записи (${j.gone})</button>` : ""}
         <button class="act" data-do="jask" data-m="all">Очистить журнал</button>`;
    return `<div class="journal sub">
      <div>Журнал в Home Assistant: ${j.events} из ${j.max_events} событий, ${j.pictures} из ${j.max_pictures} снимков${
        j.archive_from ? ` · архив регистратора с ${hhmm(j.archive_from)}` : ""}${j.gone ? ` · без записи: ${j.gone}` : ""}.
        Пределы — в настройках интеграции.</div>
      <div class="acts">${btns}</div></div>`;
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
      case "jask": this._confirm = { journal: d.m }; break;
      case "jclear":
        this._confirm = null;
        this._hass.callWS({ type: "raysharp_nvr/journal_clear", mode: d.m })
          .then(() => { this._pics.clear(); this._load(); }, (e) => { this._error = e.message || String(e); this._render(); });
        return;
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
    if (!this._open) {
      host.innerHTML = ""; host.dataset.key = ""; this._stream = null;
      this._arch = null; clearInterval(this._archTimer); return;
    }
    const n = this._nvrs.find((x) => x.entry_id === this._open.entry);
    const ch = n && n.channels.find((c) => c.num === this._open.num);
    if (!ch) { host.innerHTML = ""; return; }
    const mode = this._open.mode || (this._open.clip ? "clip" : "live");
    const act = this._active(n, ch);
    const evs = this._events.filter((e) => e.channel === ch.num).slice(0, 12);
    const has = (p) => (ch.streams || []).includes(p);
    const profiles = PROFILES.filter(([p]) => p === "auto" || has(p));
    const profile = profiles.some(([p]) => p === this._profile) ? this._profile : "auto";
    const recProfiles = REC_PROFILES.filter(([p]) => p === "main" || has(p));
    const recProfile = recProfiles.some(([p]) => p === this._recProfile) ? this._recProfile : "main";
    if (mode === "archive") this._archEnsure(ch);
    else { this._arch = null; clearInterval(this._archTimer); }
    const a = this._arch;
    const playKey = mode === "archive" ? `arch:${a && a.play ? Math.round(a.play) : "-"}`
      : mode === "clip" ? `clip:${this._open.clip}` : "live";
    const key = `${n.entry_id}:${ch.num}:${playKey}:${mode === "live" ? profile : recProfile}`;
    if (host.firstChild && host.dataset.key === key) {
      // Keep the playing video; refresh only what is around it.
      const info = host.querySelector("#info");
      if (info) info.innerHTML = this._modalInfo(n, ch, act, evs);
      // Repaint only the track: rebuilding it would cut a drag or pinch short.
      if (host.querySelector("#tl .track")) this._paintTrack(); else this._renderTimeline();
      this._bindModal(host, n, ch);
      return;
    }
    host.dataset.key = key;
    const modeTabs = `<div class="tabs modes">
        <button class="tab ${mode === "live" ? "on" : ""}" data-mode="live">● Эфир</button>
        <button class="tab ${mode === "archive" ? "on" : ""}" data-mode="archive">🕘 Архив</button>
        ${ch.talk ? `<button class="tab ${mode === "talk" ? "on" : ""}" data-mode="talk" title="Двусторонняя связь через Home Assistant (WebRTC)">🎙 Разговор</button>` : ""}
        ${mode === "clip" ? `<span class="chip">запись события ${hhmm(this._open.clip)}</span>` : ""}</div>`;
    const streamTabs = mode === "talk" ? '<span class="sub">поток выбирает Home Assistant (WebRTC)</span>'
      : mode === "live"
      ? profiles.map(([p, l]) => `<button class="tab ${p === profile ? "on" : ""}" data-prof="${p}">${l}</button>`).join("")
      : recProfiles.map(([p, l]) => `<button class="tab ${p === recProfile ? "on" : ""}" data-rprof="${p}">${l}</button>`).join("");
    host.innerHTML = `<div class="overlay" data-do="close-bg"><div class="sheet">
      <div class="shead"><b>${esc(ch.name)}</b><span class="sub">CH${ch.num}</span>
        ${mode === "live" ? '<span class="chip onair">● эфир</span>' : ""}
        <span class="grow"></span>
        <button class="x" data-do="close" aria-label="Закрыть">✕</button></div>
      ${modeTabs}
      <div class="tabs prof"><span class="sub">Поток${mode === "live" ? "" : " записи"}:</span>${streamTabs}</div>
      ${!ch.online || ch.videoloss ? `<div class="warnbar">⚠ Камера ${ch.videoloss ? "потеряла видеосигнал" : "не в сети"} — потока нет, запись с неё сейчас не идёт.</div>` : ""}
      <div id="live" class="live"></div>
      ${mode === "archive" ? '<div id="tl" class="tl"></div>' : ""}
      ${ch.ptz ? `<div class="ptzrow">
        ${this._admin ? `<div class="ptz" id="ptz">${PTZ.map(([l, c]) => c
          ? `<button class="act" data-ptz="${c}" aria-label="${c.replace("Ptz_Cmd_", "")}">${l}</button>` : "<span></span>").join("")}</div>` : ""}
        <div id="presets" class="presets"><span class="sub">Точки камеры…</span></div></div>` : ""}
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
    if (mode === "talk") {
      // Only WebRTC (no parallel HLS): a door panel serves very few streams.
      const webrtc = customElements.get("ha-web-rtc-player");
      const el = document.createElement(webrtc ? "ha-web-rtc-player" : "ha-camera-stream");
      if (st && (webrtc || customElements.get("ha-camera-stream"))) {
        el.hass = this._hass; el.muted = false; el.controls = true;
        if (webrtc) { el.entityid = ch.camera; el.autoplay = true; el.playsInline = true; }
        else { el.stateObj = st; el.allowExoPlayer = true; }
        live.innerHTML = ""; live.appendChild(el); this._stream = el;
        el.addEventListener("microphone-changed", () => show());
      }
      // HA 2026.11 keeps the talk button in its own camera dialog; the player
      // only exposes toggleMicrophone(). Offer both: talk here, or HA's dialog
      // (which the HA apps will support over http too).
      const bar = document.createElement("div");
      bar.className = "talkbar";
      const secure = window.isSecureContext;
      bar.innerHTML = `<button class="act primary" data-talk="mic" ${secure ? "" : "disabled"}>🎙 Начать говорить</button>
        <button class="act" data-talk="dialog">Окно камеры Home Assistant</button>
        <span class="sub talkstate">${secure ? "микрофон выключен" : ""}</span>`;
      live.after(bar);
      if (!secure) {
        const w = document.createElement("div");
        w.className = "warnbar";
        w.textContent = "⚠ Страница открыта по http — браузер не даёт микрофон. Откройте Home Assistant по https (адрес вида https://…srvprm.ru) или нажмите «Окно камеры Home Assistant» в приложении.";
        bar.after(w);
      }
      const micBtn = bar.querySelector('[data-talk="mic"]'), micState = bar.querySelector(".talkstate");
      const show = () => {
        const on = this._stream === el && !!el.microphoneTrack;
        micBtn.textContent = on ? "⏹ Закончить разговор" : "🎙 Начать говорить";
        micBtn.classList.toggle("on", on);
        if (secure) micState.textContent = on ? "● вас слышно у панели" : "микрофон выключен";
      };
      const caps = (c) => {
        if (!(c && c.two_way_audio) && secure) { micBtn.disabled = true; micState.textContent = "HA не разрешил разговор с этой камерой: включите «Настройки → Система → Labs → Двусторонняя связь»"; }
      };
      el.addEventListener("camera-capabilities-changed", (ev) => caps(ev.detail && ev.detail.capabilities));
      if (webrtc) this._hass.callWS({ type: "camera/capabilities", entity_id: ch.camera }).then(caps, () => {});
      micBtn.onclick = async (ev) => {
        ev.stopPropagation();
        micBtn.disabled = true;
        try { await el.toggleMicrophone(); } catch (e) { micState.textContent = `микрофон: ${e.message || e}`; }
        micBtn.disabled = false;
        setTimeout(show, 300); setTimeout(show, 1500);
      };
      bar.querySelector('[data-talk="dialog"]').onclick = (ev) => {
        ev.stopPropagation();
        this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: ch.camera }, bubbles: true, composed: true }));
      };
    } else if (mode === "archive") {
      this._renderTimeline();
      if (a && a.play) {
        const t = a.play;
        this._playAv(live, key, { camera: ch.camera, ts: t, until: t + 600, exact: true, profile: recProfile },
          null, `Открываю архив с ${hm(t, true)}…`, (v, res) => this._archVideo(v, res, t));
      } else {
        live.innerHTML = `<div class="noimg">Выберите время на шкале ниже — запись начнётся с этого момента</div>`;
      }
    } else if (mode === "clip" && ch.camera) {
      const { clip, until } = this._open;
      this._playAv(live, key, { camera: ch.camera, ts: clip, ...(until ? { until } : {}), profile: recProfile },
        recProfile === "main" ? () => this._playClip(live, ch, clip, until) : null, "Открываю запись…");
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
    if (ch.ptz && ch.camera) this._loadPresets(ch);
  }

  // --- PTZ presets: saved in the camera (pan, tilt, zoom) with a name -------------
  async _loadPresets(ch, list) {
    const box = this.shadowRoot.getElementById("presets");
    if (!box) return;
    if (!list) {
      try { list = (await this._hass.callWS({ type: "raysharp_nvr/ptz_presets", camera: ch.camera })).presets; }
      catch (e) { box.innerHTML = `<div class="warnbar">⚠ Точки камеры не прочитаны: ${esc(e.message || e)}</div>`; return; }
    }
    this._presets = { cam: ch.camera, list, edit: this._presets && this._presets.cam === ch.camera ? this._presets.edit : false,
      set: this._presets && this._presets.cam === ch.camera ? this._presets.set : null };
    this._renderPresets(ch);
    if (!this._presets.set) {
      this._hass.callWS({ type: "raysharp_nvr/ptz_settings", camera: ch.camera })
        .then((r) => { if (this._presets && this._presets.cam === ch.camera) { this._presets.set = r; this._renderPresets(ch); } }, () => {});
    }
  }

  // Hold-to-act for zoom / focus, like the arrows: Stop after Start, >= 400 ms.
  _hold(b, start, stop) {
    let run = null;
    b.addEventListener("pointerdown", (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      if (run) return;
      try { b.setPointerCapture(ev.pointerId); } catch (e) { /* old browsers */ }
      b.classList.add("on"); run = { t0: Date.now(), p: start() };
    });
    const up = async () => {
      const r = run; run = null; b.classList.remove("on");
      if (!r) return;
      await r.p; setTimeout(stop, Math.max(0, 400 - (Date.now() - r.t0)));
    };
    ["pointerup", "pointercancel", "lostpointercapture"].forEach((e) => b.addEventListener(e, up));
    b.addEventListener("contextmenu", (ev) => ev.preventDefault());
    b.onclick = (ev) => ev.stopPropagation();
  }

  _renderPresets(ch) {
    const box = this.shadowRoot.getElementById("presets");
    const P = this._presets;
    if (!box || !P) return;
    const set = P.list.filter((p) => p.set);
    const free = P.list.find((p) => !p.set);
    const chips = set.map((p) => P.edit
      ? `<span class="pchip edit"><input data-pname="${p.no}" value="${esc(p.name)}" maxlength="32" placeholder="Точка ${p.no}">
           <button class="act" data-pop="rename" data-no="${p.no}" title="Сохранить имя">✓</button>
           <button class="act" data-pop="save" data-no="${p.no}" title="Записать сюда текущее положение">⟳</button>
           <button class="act danger" data-pop="clear" data-no="${p.no}" title="Удалить точку">✕</button></span>`
      : `<button class="act pchip" data-pop="call" data-no="${p.no}">🎯 ${esc(p.name || `Точка ${p.no}`)}</button>`).join("");
    const admin = this._admin ? (P.edit
      ? `${free ? `<span class="pchip edit new"><input data-pname="new" maxlength="32" placeholder="Новая точка, напр. «На ворота»">
           <button class="act primary" data-pop="save" data-no="${free.no}">＋ Запомнить текущее положение (№${free.no})</button></span>` : ""}
         <button class="act" data-pop="done">Готово</button>`
      : `<button class="act" data-pop="edit">✎ Точки…</button>`) : "";
    // The camera's own extras: zoom, human tracking, idle return to the home preset.
    const S = P.set || { commands: [] };
    const can = (k) => (S.commands || []).includes(k);
    const homeP = S.watch_mode === "Mode_Watch_Point" ? set.find((p) => p.no === S.watch_num) : null;
    const extras = `<div class="pextra">
      ${can("zoom_in") ? `<button class="act hold" data-hold="zoom_out" title="Отдалить">🔍−</button><button class="act hold" data-hold="zoom_in" title="Приблизить">🔍＋</button>` : ""}
      ${can("track_human") ? `<button class="act" data-px="track_human" title="Камера ведёт человека в кадре">👣 Следить за человеком</button>` : ""}
      ${homeP && can("watch_stop") ? `<button class="act" data-px="watch_stop" title="Камера останется здесь">⏸ Не возвращаться</button>
         <button class="act" data-px="watch_start">↩ Возвращать</button>` : ""}
    </div>
    <div class="sub">${homeP ? `Без действий камера через ${S.watch_time || 60} с сама возвращается на «${esc(homeP.name || `Точка ${homeP.no}`)}».`
      : S.commands && S.commands.length ? "Возврат на исходную точку не настроен." : ""}</div>
    ${this._admin && P.edit && set.length && S.watch_modes && S.watch_modes.includes("Mode_Watch_Point") ? `<div class="phome">
      Исходная точка <select data-home>${['<option value="">— не возвращаться —</option>', ...set.map((p) =>
        `<option value="${p.no}" ${homeP && homeP.no === p.no ? "selected" : ""}>${esc(p.name || `Точка ${p.no}`)}</option>`)].join("")}</select>
      через <input data-homesec type="number" min="${S.watch_time_min || 15}" max="${S.watch_time_max || 240}" value="${S.watch_time || 60}"> с
      <button class="act primary" data-pop="home">Сохранить</button></div>` : ""}`;
    box.innerHTML = `<div class="plist">${chips || (P.edit ? "" : '<span class="sub">Точек ещё нет — поверните камеру и нажмите «✎ Точки…»</span>')}</div>
      <div class="pfoot">${admin}</div>${extras}
      <div class="sub pnote"></div>`;
    const note = box.querySelector(".pnote");
    const extra = (what, state = "Start") => this._hass.callWS({ type: "raysharp_nvr/ptz_extra", camera: ch.camera, what, state })
      .catch((e) => { note.textContent = `⚠ ${e.message || e}`; });
    box.querySelectorAll("[data-hold]").forEach((b) => this._hold(b, () => extra(b.dataset.hold, "Start"), () => extra(b.dataset.hold, "Stop")));
    box.querySelectorAll("[data-px]").forEach((b) => { b.onclick = async (ev) => {
      ev.stopPropagation(); b.disabled = true; await extra(b.dataset.px);
      note.textContent = { track_human: "Камера ведёт человека в кадре", watch_stop: "Камера останется здесь, пока не нажмёте «Возвращать»", watch_start: "Камера вернётся на исходную точку" }[b.dataset.px] || "";
      b.disabled = false;
    }; });
    box.querySelectorAll("[data-pop]").forEach((b) => { b.onclick = async (ev) => {
      ev.stopPropagation();
      const op = b.dataset.pop, no = Number(b.dataset.no);
      if (op === "edit" || op === "done") { P.edit = op === "edit"; this._renderPresets(ch); return; }
      if (op === "home") {
        const sel = box.querySelector("[data-home]"), sec = box.querySelector("[data-homesec]");
        b.disabled = true; note.textContent = "Сохраняю в камере…";
        try {
          P.set = await this._hass.callWS({ type: "raysharp_nvr/ptz_home", camera: ch.camera,
            no: sel.value ? Number(sel.value) : null, seconds: Number(sec.value) || 60 });
          this._renderPresets(ch);
        } catch (e) { note.textContent = `⚠ ${e.message || e}`; b.disabled = false; }
        return;
      }
      if (op === "clear" && b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = "Удалить?"; return; }
      const inp = box.querySelector(`[data-pname="${op === "save" && !P.list.find((p) => p.no === no && p.set) ? "new" : no}"]`);
      const name = inp ? inp.value.trim() : "";
      b.disabled = true;
      note.textContent = op === "call" ? "Поворачиваю…" : "Сохраняю в камере…";
      try {
        const r = await this._hass.callWS({ type: "raysharp_nvr/ptz_preset", camera: ch.camera, no, op, name });
        if (op !== "call") { P.list = r.presets; this._renderPresets(ch); return; }
        note.textContent = "";
      } catch (e) { note.textContent = `⚠ ${e.message || e}`; }
      b.disabled = false;
    }; });
  }

  // --- archive ----------------------------------------------------------------
  _archEnsure(ch) {
    const a = this._arch;
    if (a && a.cam === ch.camera) return;
    clearInterval(this._archTimer);
    const today = dayStr(new Date());
    this._arch = { cam: ch.camera, day: today, data: null, err: "", span: 86400, center: null, play: null, video: null, chunk: null };
    this._archLoad();
    this._archTimer = setInterval(() => this._archTick(), 1000);
  }

  async _archLoad() {
    const a = this._arch;
    if (!a) return;
    a.data = null; a.err = ""; this._renderTimeline();
    try {
      const d = await this._hass.callWS({ type: "raysharp_nvr/timeline", camera: a.cam, day: a.day });
      if (this._arch !== a) return;
      a.data = d;
      if (a.center === null || a.center < d.start || a.center >= d.end) a.center = (d.start + d.end) / 2;
    } catch (e) {
      if (this._arch !== a) return;
      a.err = e.message || String(e);
    }
    this._renderTimeline();
  }

  _archWindow() {
    const a = this._arch, d = a.data;
    const span = Math.min(a.span, d.end - d.start);
    let w0 = (a.center ?? d.start) - span / 2;
    w0 = Math.max(d.start, Math.min(d.end - span, w0));
    return [w0, w0 + span];
  }

  _archMarks() {
    const d = this._arch && this._arch.data;
    if (!d) return [];
    const m = [...d.segments.filter((x) => x.kind !== "normal").map((x) => x.s), ...d.events.map((e) => e.ts)];
    return [...new Set(m.map((t) => Math.round(t)))].sort((x, y) => x - y);
  }

  _archPlay(ts) {
    const a = this._arch, d = a && a.data;
    if (!d) return;
    // Nothing recorded at that moment: start at the next recorded piece.
    const inside = d.segments.some((x) => x.s <= ts && ts < x.e);
    if (!inside) {
      const next = d.segments.find((x) => x.s > ts);
      if (!next) { a.err = `После ${hm(ts)} записей за этот день нет`; this._renderTimeline(); return; }
      ts = next.s;
    }
    a.err = ""; a.play = ts; a.pos = ts;
    this._renderModal();
  }

  _archVideo(v, res, start) {
    const a = this._arch;
    if (!a) return;
    a.video = v; a.chunk = { start: res.start ?? start, duration: res.duration || 600 };
    v.addEventListener("ended", () => {
      // The archive plays in 10-minute pieces: go on with the next one.
      if (this._arch === a && a.chunk) this._archPlay(a.chunk.start + a.chunk.duration);
    });
  }

  _archTick() {
    const a = this._arch;
    if (!a || !a.video || !a.chunk || !a.video.isConnected) return;
    a.pos = a.chunk.start + (a.video.currentTime || 0);
    const d = a.data;
    if (d && a.span < d.end - d.start && Date.now() - (a.userAt || 0) > 8000) {
      const [w0, w1] = this._archWindow();
      if (a.pos > w1 - (w1 - w0) * 0.05) { a.center = a.pos + (w1 - w0) * 0.4; this._paintTrack(); return; } // follow the playhead
    }
    this._moveCursor();
  }

  _moveCursor() {
    const a = this._arch, tl = this.shadowRoot.getElementById("tl");
    if (!a || !a.data || !tl) return;
    const cur = tl.querySelector(".cur"), lab = tl.querySelector(".poslabel");
    const [w0, w1] = this._archWindow();
    if (cur && a.pos) { cur.style.left = `${((a.pos - w0) / (w1 - w0)) * 100}%`; cur.hidden = a.pos < w0 || a.pos > w1; }
    if (lab && a.pos) lab.textContent = hm(a.pos, true);
  }

  // Segments, event ticks, playhead and hour labels for the visible window.
  _trackParts() {
    const a = this._arch, d = a.data;
    const [w0, w1] = this._archWindow();
    const span = w1 - w0;
    const pct = (t) => ((t - w0) / span) * 100;
    const segs = d.segments.filter((x) => x.e > w0 && x.s < w1).map((x) => {
      const l = Math.max(0, pct(x.s)), r = Math.min(100, pct(x.e));
      return `<div class="seg ${x.kind}" style="left:${l}%;width:${Math.max(0.15, r - l)}%" title="${SEG[x.kind] || x.kind} ${hm(x.s, true)}–${hm(x.e, true)}"></div>`;
    }).join("");
    const ticks = d.events.filter((e) => e.ts >= w0 && e.ts <= w1).map((e) =>
      `<div class="evtick" style="left:${pct(e.ts)}%" title="${esc(TYPE[e.type] || e.type)} ${hm(e.ts, true)}"></div>`).join("");
    const step = [60, 300, 600, 1800, 3600, 7200, 10800].find((st) => span / st <= 12) || 10800;
    const labels = [];
    for (let t = Math.ceil(w0 / step) * step; t <= w1; t += step) {
      labels.push(`<span class="hl" style="left:${pct(t)}%">${hm(t)}</span>`);
    }
    const cur = `<div class="cur" style="left:${a.pos ? pct(a.pos) : -10}%" ${a.pos && a.pos >= w0 && a.pos <= w1 ? "" : "hidden"}></div>`;
    return { inner: segs + ticks + cur, labels: labels.join("") };
  }

  _paintTrack() {
    const a = this._arch, tl = this.shadowRoot.getElementById("tl");
    const track = tl && tl.querySelector(".track"), hours = tl && tl.querySelector(".hours");
    if (!a || !a.data || !track || !hours) return;
    const { inner, labels } = this._trackParts();
    track.innerHTML = inner; hours.innerHTML = labels;
    tl.querySelectorAll("[data-span]").forEach((b) => b.classList.toggle("on", Number(b.dataset.span) === a.span));
  }

  // Zoom keeping the moment under the finger/cursor where it is.
  _zoomTo(span, anchor, frac) {
    const a = this._arch, d = a.data;
    span = Math.max(120, Math.min(d.end - d.start, span));
    a.span = span;
    a.center = anchor - frac * span + span / 2;
    a.userAt = Date.now();
    this._paintTrack();
  }

  _renderTimeline() {
    const a = this._arch, tl = this.shadowRoot.getElementById("tl");
    if (!a || !tl) return;
    const d = a.data;
    const head = `<div class="tlbar">
        <button class="act" data-arch="day-">◀</button>
        <input type="date" class="tldate" value="${esc(a.day)}" max="${dayStr(new Date())}">
        <button class="act" data-arch="day+" ${a.day >= dayStr(new Date()) ? "disabled" : ""}>▶</button>
        <span class="sep"></span>
        ${SPANS.map(([v, l]) => `<button class="tab ${a.span === v ? "on" : ""}" data-span="${v}">${l}</button>`).join("")}
        <span class="sep"></span>
        <button class="act" data-arch="pan-" title="Раньше">‹</button>
        <button class="act" data-arch="pan+" title="Позже">›</button>
        <span class="sep"></span>
        <button class="act" data-arch="prev" title="Предыдущее событие">⏮ событие</button>
        <button class="act" data-arch="next" title="Следующее событие">событие ⏭</button>
        <span class="grow"></span>
        <b class="poslabel">${a.pos ? hm(a.pos, true) : ""}</b>
      </div>`;
    if (a.err && !d) { tl.innerHTML = head + `<div class="warnbar">${esc(a.err)}</div>`; this._bindTimeline(); return; }
    if (!d) { tl.innerHTML = head + '<div class="sub">Ищу записи за день…</div>'; this._bindTimeline(); return; }
    const { inner, labels } = this._trackParts();
    const rec = d.segments.reduce((sum, x) => sum + (x.e - x.s), 0);
    tl.innerHTML = head + `
      <div class="track" title="Нажмите — смотреть с этого места; колесо или два пальца — масштаб; перетаскивание — прокрутка">${inner}</div>
      <div class="hours">${labels}</div>
      <div class="legend sub">
        ${Object.entries(SEG).map(([k, l]) => `<span><i class="seg ${k}"></i>${l}</span>`).join("")}
        <span><i class="evtick"></i>событие журнала</span>
        <span class="grow"></span>
        ${d.segments.length ? `записано за день ${Math.round(rec / 3600 * 10) / 10} ч` : '<b class="warn">за этот день записей нет</b>'}
      </div>
      <div class="sub tlhint">Колесо мыши или два пальца — приблизить/отдалить, перетащить шкалу — раньше/позже, нажать — смотреть.</div>
      ${a.err ? `<div class="warnbar">${esc(a.err)}</div>` : ""}`;
    this._bindTimeline();
  }

  // Mouse: wheel zooms around the cursor (shift/trackpad sideways scrolls),
  // drag scrolls. Touch: one finger scrolls, two fingers pinch. A tap plays.
  _bindGestures(track) {
    const ptrs = new Map();
    let g = null;
    const frac = (x) => { const r = track.getBoundingClientRect(); return Math.max(0, Math.min(1, (x - r.left) / r.width)); };
    const at = (f) => { const [w0, w1] = this._archWindow(); return w0 + f * (w1 - w0); };
    track.addEventListener("pointerdown", (ev) => {
      if (!this._arch || !this._arch.data) return;
      ev.stopPropagation();
      try { track.setPointerCapture(ev.pointerId); } catch (e) { /* synthetic pointer */ }
      ptrs.set(ev.pointerId, ev.clientX);
      const [w0, w1] = this._archWindow();
      if (ptrs.size === 1) g = { x: ev.clientX, center: (w0 + w1) / 2, span: w1 - w0, moved: false };
      else if (ptrs.size === 2) {
        const [x1, x2] = [...ptrs.values()];
        const f = frac((x1 + x2) / 2);
        g = { pinch: Math.max(10, Math.abs(x1 - x2)), span: w1 - w0, anchor: at(f), moved: true };
      }
    });
    track.addEventListener("pointermove", (ev) => {
      if (!ptrs.has(ev.pointerId) || !g) return;
      ptrs.set(ev.pointerId, ev.clientX);
      const a = this._arch;
      if (g.pinch && ptrs.size >= 2) {
        const [x1, x2] = [...ptrs.values()];
        this._zoomTo(g.span * g.pinch / Math.max(10, Math.abs(x1 - x2)), g.anchor, frac((x1 + x2) / 2));
        return;
      }
      if (g.pinch) return;
      const dx = ev.clientX - g.x;
      if (Math.abs(dx) > 5) g.moved = true;
      if (!g.moved) return;
      a.center = g.center - (dx / track.getBoundingClientRect().width) * g.span;
      a.userAt = Date.now();
      this._paintTrack();
    });
    const up = (ev) => {
      if (!ptrs.has(ev.pointerId)) return;
      ptrs.delete(ev.pointerId);
      if (ev.type === "pointerup" && g && !g.moved && !g.pinch && ptrs.size === 0) this._archPlay(at(frac(ev.clientX)));
      if (ptrs.size === 0) g = null;
      else if (g && g.pinch) { const [x] = [...ptrs.values()]; const [w0, w1] = this._archWindow(); g = { x, center: (w0 + w1) / 2, span: w1 - w0, moved: true }; }
    };
    track.addEventListener("pointerup", up);
    track.addEventListener("pointercancel", up);
    track.addEventListener("click", (ev) => ev.stopPropagation());
    track.addEventListener("wheel", (ev) => {
      const a = this._arch;
      if (!a || !a.data) return;
      ev.preventDefault(); ev.stopPropagation();
      const [w0, w1] = this._archWindow();
      const sideways = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) || ev.shiftKey;
      if (sideways) {
        const delta = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
        a.center = (w0 + w1) / 2 + (delta / track.getBoundingClientRect().width) * (w1 - w0);
        a.userAt = Date.now();
        this._paintTrack();
        return;
      }
      const f = frac(ev.clientX);
      this._zoomTo((w1 - w0) * Math.exp(ev.deltaY * 0.0025), at(f), f);
    }, { passive: false });
  }

  _bindTimeline() {
    const a = this._arch, tl = this.shadowRoot.getElementById("tl");
    if (!a || !tl) return;
    const track = tl.querySelector(".track");
    if (track) this._bindGestures(track);
    const date = tl.querySelector(".tldate");
    if (date) date.onchange = (ev) => {
      ev.stopPropagation();
      if (!date.value) return;
      a.day = date.value; a.center = null; a.play = null; a.pos = null; this._archLoad(); this._renderModal();
    };
    tl.querySelectorAll("[data-span]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      a.span = Number(b.dataset.span);
      if (a.pos) a.center = a.pos;
      a.userAt = Date.now();
      this._paintTrack();
    }; });
    tl.querySelectorAll("[data-arch]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      const k = b.dataset.arch;
      if (k === "day-" || k === "day+") {
        const d = new Date(`${a.day}T12:00:00`);
        d.setDate(d.getDate() + (k === "day-" ? -1 : 1));
        a.day = dayStr(d); a.center = null; a.play = null; a.pos = null; this._archLoad(); this._renderModal();
        return;
      }
      if (!a.data) return;
      if (k === "pan-" || k === "pan+") {
        const [w0, w1] = this._archWindow();
        a.center = (w0 + w1) / 2 + (k === "pan-" ? -1 : 1) * (w1 - w0) / 2;
        a.userAt = Date.now();
        this._paintTrack();
        return;
      }
      const marks = this._archMarks();
      const at = a.pos || (k === "next" ? a.data.start : a.data.end);
      const t = k === "next" ? marks.find((m) => m > at + 1) : [...marks].reverse().find((m) => m < at - 3);
      if (t === undefined) { a.err = k === "next" ? "Дальше событий за этот день нет" : "Раньше событий за этот день нет"; this._renderTimeline(); return; }
      a.center = t;
      this._archPlay(Math.max(a.data.start, t - 5)); // a few seconds before the event
    }; });
  }

  // MP4 from the integration with the camera's sound re-encoded to AAC:
  // HA's HLS drops the G.711 audio these cameras send. Falls back to HLS
  // (silent) when ffmpeg cannot open the source or the browser cannot play it.
  async _playAv(box, key, msg, fallback, wait, onVideo) {
    const host = () => this.shadowRoot && this.shadowRoot.getElementById("modal");
    const current = () => box.isConnected && host() && host().dataset.key === key;
    box.innerHTML = `<div class="noimg">${wait}</div>`;
    let res;
    try {
      res = await this._hass.callWS({ type: "raysharp_nvr/av", ...msg, format: APPLE ? "hls" : "mp4" });
    } catch (e) {
      if (!current()) return;
      if (!fallback || (e && e.code === "no_recording")) {
        this._streamFail(box, e.message || e.code || String(e), msg.ts !== undefined);
        return;
      }
      fallback(); this._avNote(box, `🔇 видео без звука: ${e.message || e.code || e}`); return;
    }
    if (!current()) return;
    const v = document.createElement("video");
    let playing = false;
    v.addEventListener("playing", () => { playing = true; box.parentNode && box.parentNode.querySelectorAll(".warnbar.stall").forEach((n) => n.remove()); });
    setTimeout(() => {
      if (!playing && !done && current()) {
        this._streamFail(box, `Поток не подключился за ${STALL_S} с — регистратор не отдаёт видео этого потока.`, msg.ts !== undefined, true);
      }
    }, STALL_S * 1000);
    if (onVideo) onVideo(v, res);
    v.controls = true; v.autoplay = true; v.playsInline = true; v.muted = false;
    let done = false;
    v.addEventListener("error", () => {
      if (done || !current()) return;
      done = true;
      box.parentNode.querySelectorAll(".cliphint").forEach((n) => n.remove());
      const why = v.error ? `код ${v.error.code}${v.error.message ? `, ${v.error.message}` : ""}` : "";
      if (!fallback) { this._streamFail(box, `Браузер не смог воспроизвести поток (${why}).`, msg.ts !== undefined); return; }
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
    if (res.before !== undefined && !msg.exact) {
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

  // "The stream did not connect" with what to do next: try again or another stream.
  _streamFail(box, text, recording, stall = false) {
    if (!box.isConnected) return;
    const wrap = box.parentNode;
    wrap.querySelectorAll(".warnbar.fail").forEach((n) => n.remove());
    const cur = recording ? this._recProfile : this._profile;
    const alt = (recording ? REC_PROFILES : PROFILES).filter(([p]) => p !== cur && p !== "auto");
    const bar = document.createElement("div");
    bar.className = `warnbar fail${stall ? " stall" : ""}`;
    bar.innerHTML = `<div>⚠ <b>Поток не подключён.</b> ${esc(text)}</div>
      <div class="acts"><button class="act" data-retry="1">Повторить</button>
        ${alt.map(([p, l]) => `<button class="act" data-alt="${p}">Поток «${l}»</button>`).join("")}</div>`;
    if (!stall) box.innerHTML = '<div class="noimg">нет видео</div>';
    box.after(bar);
    bar.querySelector("[data-retry]").onclick = (ev) => {
      ev.stopPropagation();
      const host = this.shadowRoot.getElementById("modal"); host.dataset.key = ""; this._renderModal();
    };
    bar.querySelectorAll("[data-alt]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      if (recording) { this._recProfile = b.dataset.alt; store.set("rec_profile", this._recProfile); }
      else { this._profile = b.dataset.alt; store.set("profile", this._profile); }
      this._renderModal();
    }; });
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
    return `<div class="hrow wrap">
        ${act.length ? act.map((t) => `<span class="chip on">${esc(TYPE[t] || t)}</span>`).join("") : '<span class="sub">сейчас спокойно</span>'}
        ${ch.recording === true ? '<span class="chip">● запись</span>' : ch.recording === false ? '<span class="chip">запись не идёт</span>' : ""}
        <span class="grow"></span>
        ${this._admin ? `<button class="act" data-do="snap">📷 Снимок</button>` : ""}
      </div>
      <h3>События этой камеры — нажмите, чтобы посмотреть запись</h3>
      <div class="evrow">${evs.map((e) => {
        const pic = this._pics.get(e.id);
        const on = this._open.clip === e.ts;
        if (e.gone) {
          return `<div class="evc gone">${pic ? `<img src="${pic}" alt="">` : `<div class="noimg sm">—</div>`}
          <div class="sub">${esc(TYPE[e.type] || e.type)} · ${hhmm(e.ts)} · <span class="warn">запись удалена</span></div></div>`;
        }
        return `<button class="evc ${on ? "on" : ""}" data-clip="${e.ts}" data-end="${e.rec_end || ""}">${pic ? `<img src="${pic}" alt="">` : `<div class="noimg sm">▶</div>`}
          <div class="sub">▶ ${esc(TYPE[e.type] || e.type)}${e.detail ? ` ${esc(e.detail)}` : ""} · ${hhmm(e.ts)}</div></button>`;
      }).join("") || '<span class="sub">нет</span>'}</div>`;
  }

  _bindModal(host, n, ch) {
    host.querySelectorAll("[data-do]").forEach((b) => { b.onclick = (ev) => {
      if (b.dataset.do === "close-bg" && ev.target !== b) return;
      ev.stopPropagation();
      if (b.dataset.do === "close" || b.dataset.do === "close-bg") { this._open = null; this._renderModal(); }
      if (b.dataset.do === "live") { this._open = { ...this._open, clip: null, until: null, mode: "live" }; this._renderModal(); }
      if (b.dataset.do === "snap") this._act({ type: "raysharp_nvr/action", entry_id: n.entry_id, action: "snapshot", channel: ch.num }, "snap");
    }; });
    host.querySelectorAll("[data-mode]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      this._open = { ...this._open, clip: null, until: null, mode: b.dataset.mode }; this._renderModal();
    }; });
    host.querySelectorAll("[data-rprof]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      this._recProfile = b.dataset.rprof; store.set("rec_profile", this._recProfile); this._renderModal();
    }; });
    host.querySelectorAll("[data-prof]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      this._profile = b.dataset.prof; store.set("profile", this._profile); this._renderModal();
    }; });
    host.querySelectorAll("[data-clip]").forEach((b) => { b.onclick = (ev) => {
      ev.stopPropagation();
      this._open = { ...this._open, clip: Number(b.dataset.clip), until: b.dataset.end ? Number(b.dataset.end) : null, mode: "clip" };
      this._renderModal();
      host.querySelector(".sheet")?.scrollIntoView({ block: "start" });
    }; });
    // PTZ: hold to move, release to stop. A quick tap on a phone still moves a
    // step: Stop waits for Start to reach the NVR and for at least 400 ms
    // (otherwise Stop could even arrive first and the camera never stops).
    host.querySelectorAll("[data-ptz]").forEach((b) => {
      if (b.dataset.bound) return;
      b.dataset.bound = "1";
      const send = (state) => this._hass.callWS({ type: "raysharp_nvr/action", entry_id: n.entry_id,
        action: "ptz", channel: ch.num, command: b.dataset.ptz, state }).catch((e) => {
          const box = host.querySelector("#ptz");
          if (!box) return;
          let note = box.nextElementSibling && box.nextElementSibling.classList.contains("ptzerr") ? box.nextElementSibling : null;
          if (!note) { note = document.createElement("div"); note.className = "warnbar ptzerr"; box.after(note); }
          note.textContent = `⚠ Поворот не выполнен: ${e.message || e}`;
        });
      let run = null;
      const down = (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        if (run) return;
        try { b.setPointerCapture(ev.pointerId); } catch (e) { /* old browsers */ }
        b.classList.add("on");
        run = { t0: Date.now(), started: send("Start") };
      };
      const up = async (ev) => {
        if (ev) ev.stopPropagation();
        const r = run; run = null;
        b.classList.remove("on");
        if (!r) return;
        await r.started;
        setTimeout(() => send("Stop"), Math.max(0, 400 - (Date.now() - r.t0)));
      };
      b.addEventListener("pointerdown", down);
      b.addEventListener("pointerup", up);
      b.addEventListener("pointercancel", up);
      b.addEventListener("lostpointercapture", up);
      b.addEventListener("contextmenu", (ev) => ev.preventDefault());
      b.onclick = (ev) => ev.stopPropagation();
    });
  }
}

const STYLE = `
  :host { display:block; background: var(--primary-background-color); color: var(--primary-text-color);
    min-height: 100vh; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); overflow-x: hidden; }
  .toolbar { display:flex; align-items:center; height:56px; padding:0 12px; gap:8px;
    background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); }
  .title { font-size: 20px; }
  .body { padding: clamp(8px, 1.2vw, 32px); margin: 0 auto; box-sizing: border-box; --z: 1; }
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
  .layout { display:grid; grid-template-columns: 1fr clamp(320px, 20%, 480px); gap: 12px; align-items: start; }
  .grid { display:grid; gap: 8px; }
  /* At least the chosen tile size, and at most 6 / 4 / 3 columns, so tiles
     grow with the screen instead of leaving it empty. */
  .grid.s { grid-template-columns: repeat(auto-fill, minmax(max(180px, calc((100% - 5 * 8px) / 6)), 1fr)); }
  .grid.m { grid-template-columns: repeat(auto-fill, minmax(max(260px, calc((100% - 3 * 8px) / 4)), 1fr)); }
  .grid.l { grid-template-columns: repeat(auto-fill, minmax(max(420px, calc((100% - 2 * 8px) / 3)), 1fr)); }
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
  .prof { margin-bottom: 8px; align-items: center; }
  .ev.gone, .evc.gone { opacity: .7; }
  .journal { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--divider-color); }
  .journal .acts { display:flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; align-items: center; }
  .talkbar { display:flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0; }
  .talkbar .act { font-size: 15px; padding: 8px 14px; }
  .talkbar .act.on { background: var(--error-color, #c62828); border-color: var(--error-color, #c62828); color: #fff; }
  .modes { margin-bottom: 6px; align-items: center; }
  .warnbar { margin: 8px 0; padding: 8px 12px; border-radius: 8px; font-size: 13px;
    border-left: 4px solid var(--warning-color, #ffa600); background: var(--secondary-background-color); }
  .warnbar .acts { display:flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .tl { margin-top: 10px; }
  .tlbar { display:flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 8px; }
  .tlbar .sep { width: 6px; }
  .tldate { padding: 4px 8px; border-radius: 8px; border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); color: var(--primary-text-color); font-size: 13px; color-scheme: light dark; }
  .track { position: relative; height: 40px; border-radius: 6px; background: var(--secondary-background-color); cursor: grab;
    overflow: hidden; touch-action: none; user-select: none; -webkit-user-select: none; }
  .track:active { cursor: grabbing; }
  .track .seg, .track .evtick, .track .cur { pointer-events: none; }
  .tlhint { margin-top: 4px; }
  .seg { position: absolute; top: 0; bottom: 0; background: #607d8b; }
  .seg.motion { background: #ff9800; top: 6px; bottom: 6px; }
  .seg.smart { background: #e53935; top: 4px; bottom: 4px; }
  .seg.pir { background: #8e24aa; top: 6px; bottom: 6px; }
  .seg.io { background: #8e24aa; top: 6px; bottom: 6px; }
  .evtick { position: absolute; top: 0; width: 2px; height: 10px; margin-left: -1px; background: #ffeb3b; }
  .cur { position: absolute; top: -2px; bottom: -2px; width: 2px; margin-left: -1px; background: #fff; box-shadow: 0 0 3px #000; }
  .hours { position: relative; height: 16px; font-size: 11px; color: var(--secondary-text-color); margin-top: 2px; }
  .hours .hl { position: absolute; transform: translateX(-50%); white-space: nowrap; }
  .legend { display:flex; flex-wrap: wrap; gap: 4px 12px; align-items: center; margin-top: 4px; }
  .legend i { display:inline-block; position: static; width: 14px; height: 8px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
  .legend i.evtick { width: 3px; height: 10px; }
  .warn { color: var(--warning-color, #ffa600); }
  .noimg { width: 100%; height: 100%; display:flex; align-items:center; justify-content:center; color: #bbb; font-size: 13px; aspect-ratio: 16/9; background: #222; }
  .noimg.sm { aspect-ratio: 16/9; }
  .badge { position: absolute; left: 6px; top: 6px; background: var(--error-color, #c62828); color: #fff; font-size: 12px; font-weight: 600;
    padding: 2px 8px; border-radius: 10px; animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .6; } }
  .rec { position: absolute; right: 6px; top: 6px; background: rgba(0,0,0,.55); color: #ff5252; font-size: 11px; padding: 1px 6px; border-radius: 8px; }
  .cap { display:flex; align-items: baseline; justify-content: space-between; gap: 6px; padding: 6px 8px; }
  .cap b { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .feed { position: sticky; top: 8px; max-height: calc((100vh - 90px) / var(--z, 1)); overflow-y: auto; }
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
  .ptz { grid-template-columns: repeat(3, 56px) !important; gap: 6px !important; }
  .ptz .act { padding: 12px 0; font-size: 20px; touch-action: none; user-select: none; -webkit-user-select: none;
    -webkit-touch-callout: none; -webkit-tap-highlight-color: transparent; }
  .ptz .act.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  /* Arrows on the left, the camera's presets as big buttons to the right; on a
     narrow phone the presets drop under the arrows at full width. */
  .ptzrow { display:flex; flex-wrap: wrap; gap: 12px; align-items: flex-start; margin-top: 10px; }
  .ptzrow .ptz { margin-top: 0; flex: none; }
  .presets { flex: 1 1 180px; min-width: 0; }
  .plist { display:grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px; }
  .plist .pchip:not(.edit) { padding: 12px 10px; font-size: 15px; text-align: left; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; touch-action: manipulation; }
  .pfoot { display:flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .pchip.edit { display:flex; gap: 4px; align-items:center; padding: 3px; border: 1px solid var(--divider-color); border-radius: 8px;
    grid-column: 1 / -1; }
  .pchip.edit input { flex: 1; min-width: 0; padding: 8px; border-radius: 6px; border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); color: var(--primary-text-color); font-size: 15px; }
  .pchip.edit .act { padding: 8px 10px; font-size: 15px; }
  .pfoot .pchip.new { flex: 1 1 100%; }
  .pextra { display:flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 4px; }
  .pextra .act { padding: 10px 12px; font-size: 15px; touch-action: none; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; }
  .pextra .act.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .phome { display:flex; flex-wrap: wrap; gap: 6px; align-items:center; margin-top: 8px; font-size: 14px; }
  .phome select, .phome input { padding: 6px 8px; border-radius: 6px; border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); color: var(--primary-text-color); font-size: 14px; }
  .phome input { width: 70px; }
  .act.danger { border-color: var(--error-color, #c62828); color: var(--error-color, #c62828); }
  .evrow { display:grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
  .evc img { width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 6px; display:block; }
  /* Big screens: use the whole width and scale up, so a 4K monitor is not a
     narrow strip of small text. The phone and laptop layout stays as it is. */
  @media (min-width: 2000px) { .body, .sheet { zoom: 1.15; } .body { --z: 1.15; } }
  @media (min-width: 2600px) { .body, .sheet { zoom: 1.3; } .body { --z: 1.3; } }
  @media (min-width: 3400px) { .body, .sheet { zoom: 1.5; } .body { --z: 1.5; } }
  @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } .feed { position: static; max-height: none; } }
  @media (max-width: 600px) {
    .overlay { padding: 0; background: var(--card-background-color); }
    .sheet { border-radius: 0; min-height: 100%; padding: calc(8px + env(safe-area-inset-top)) 10px calc(16px + env(safe-area-inset-bottom)); }
    .shead { top: calc(-8px - env(safe-area-inset-top)); padding-top: calc(8px + env(safe-area-inset-top)); margin-top: calc(-8px - env(safe-area-inset-top)); }
    .body { padding: 8px; } .grid.m, .grid.l { grid-template-columns: 1fr; } .grid.s { grid-template-columns: repeat(2, minmax(0,1fr)); } }
`;

if (!customElements.get("raysharp-nvr-panel")) customElements.define("raysharp-nvr-panel", RaySharpNvrPanel);
