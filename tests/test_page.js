// Runs pi/control.html's script with a stub DOM, fetch, WebSocket and timers.   node tests/test_page.js
const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "pi", "control.html"), "utf8");
const src = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));

const JSON_TYPE = "application/json; charset=utf-8";
const HTML_TYPE = "text/html";
const CODE = "link-code-0123456789";
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };

function mk(tag) {
  const n = { tag, children: [], parent: null, textContent: "", hidden: false, className: "", src: "", value: "",
    attrs: {}, dataset: {}, style: {}, rect: null, naturalWidth: 0, naturalHeight: 0, on: {},
    append(...c) { for (const k of c) { k.remove(); k.parent = this; this.children.push(k); } },
    replaceChildren(...c) { for (const k of this.children) k.parent = null; this.children = []; this.append(...c); },
    remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; },
    addEventListener(t, f) { this.on[t] = f; },
    setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; },
    getBoundingClientRect() { return this.rect || { left: 0, top: 0, width: 0, height: 0 }; } };
  const has = (c) => n.className.split(" ").includes(c);
  n.classList = { contains: has, add: (c) => { if (!has(c)) n.className = `${n.className} ${c}`.trim(); },
    remove: (c) => { n.className = n.className.split(" ").filter((x) => x && x !== c).join(" "); } };
  return n;
}
const all = (n) => [n, ...n.children.flatMap(all)];
const text = (n) => n.textContent + n.children.map(text).join("");
const label = (n) => n.attrs["aria-label"] || text(n);
const has = (n, c) => n.className.split(" ").includes(c);

async function boot(href, routes, opts = {}) {
  const u = new URL(href);
  const fetched = [], posts = [], replaced = [], sockets = [], confirms = [], revoked = [], blobs = [];
  const els = { login: mk("form"), loginErr: mk("p"), code: mk("input"), hosts: mk("div"), toast: mk("div"),
    conn: mk("span"), logout: mk("button") };
  els.login.hidden = els.logout.hidden = els.conn.hidden = true;
  const docOn = {};
  const document = { getElementById: (id) => els[id], createElement: mk, hidden: false,
    addEventListener: (t, f) => { docOn[t] = f; } };
  const location = { href: u.href, origin: u.origin, protocol: u.protocol, host: u.host, pathname: u.pathname,
    search: u.search, hash: u.hash };
  const history = { replaceState: (a, b, url) => replaced.push(url) };
  const store = "store" in opts ? opts.store : { kioskCode: "saved-code-0123456789" };
  const localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; },
    removeItem: (k) => { delete store[k]; } };

  let now = 0, tid = 0;
  const timers = [];
  const setTimeout = (f, ms = 0) => { timers.push({ id: ++tid, at: now + ms, f }); return tid; };
  const setInterval = (f, ms) => { timers.push({ id: ++tid, at: now + ms, f, every: ms }); return tid; };
  const clearTimeout = (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); };
  async function tick(ms) {
    const end = now + ms;
    for (let due; (due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0]);) {
      now = due.at;
      if (due.every) due.at += due.every; else timers.splice(timers.indexOf(due), 1);
      due.f();
      await settle();
    }
    now = end;
    await settle();
  }

  const flat = (n) => [has(n, "name") || has(n, "off") ? n.textContent : null, ...n.children.flatMap(flat)].filter(Boolean);
  const env = { fetched, posts, replaced, sockets, confirms, revoked, blobs, store, els, tick, settle,
    confirmAnswer: true, failWs: !!opts.failWs,
    get rendered() { return flat(els.hosts); },
    get ws() { return sockets[sockets.length - 1]; },
    find: (pred) => all(els.hosts).find(pred),
    btn: (l, nth = 0) => all(els.hosts).filter((n) => n.tag === "button" && label(n) === l)[nth],
    input: (l) => all(els.hosts).find((n) => n.tag === "input" && n.attrs["aria-label"] === l),
    card: (host) => all(els.hosts).find((n) => n.tag === "section" && n.children[0] && text(n.children[0]).startsWith(host)),
    setHidden: (h) => { document.hidden = h; docOn.visibilitychange(); },
    commands: () => sockets.flatMap((s) => s.sent).filter((m) => m.type === "command"),
    // the phone WebSocket after auth: hello, then one state per Pi
    async live(states, ws = sockets[sockets.length - 1]) {
      ws.open();
      ws.push({ type: "hello", pis: Object.keys(states).map((h) => ({ host: h, online: !!states[h] })) });
      for (const [h, s] of Object.entries(states)) ws.push({ type: "state", host: h, online: !!s, state: s });
      await settle();
      return ws;
    } };
  const fetch = async (url, o = {}) => {
    const method = o.method || "GET";
    fetched.push(url);
    if (env.gate) await env.gate;
    if (method === "POST") posts.push({ url, body: o.body === undefined ? undefined : JSON.parse(o.body), code: o.headers["X-Code"] });
    const abs = new URL(url, u.href);
    const key = (abs.origin === u.origin ? "" : abs.origin) + abs.pathname;
    let r = routes[key];
    if (!r && method === "POST" && /\/api\/screen\/\d+\/[a-z]+$/.test(abs.pathname)) r = [200, JSON_TYPE, st("kleros1")];
    const [status, type, body] = r || [404, JSON_TYPE, { error: "not found" }];
    return { ok: status >= 200 && status < 300, status, statusText: { 403: "Forbidden", 502: "Bad Gateway" }[status] || "",
      headers: { get: (h) => (h.toLowerCase() === "content-type" ? type : null) },
      json: async () => { if (typeof body === "string") throw new SyntaxError("Unexpected token '<'"); return body; },
      blob: async () => "blob" };
  };
  class PageURL extends URL {
    static createObjectURL(b) { blobs.push(b); return `blob:${blobs.length}`; }
    static revokeObjectURL(x) { revoked.push(x); }
  }
  class WebSocket {
    constructor(url) {
      Object.assign(this, { url, sent: [], readyState: 0, created: now });
      sockets.push(this);
      if (env.failWs) setTimeout(() => this.shut(1006), 0);
    }
    send(d) { if (this.readyState !== 1) throw new Error("not open"); this.sent.push(JSON.parse(d)); }
    close() { this.readyState = 3; this.closed = true; }
    open() { this.readyState = 1; this.onopen({}); }
    push(m) { this.onmessage({ data: m instanceof Uint8Array ? m.buffer : JSON.stringify(m) }); }
    shut(code) { this.readyState = 3; this.onclose({ code }); }
  }
  new Function("document", "location", "history", "localStorage", "fetch", "setInterval", "setTimeout", "clearTimeout",
    "confirm", "URL", "WebSocket", "Blob", src)(document, location, history, localStorage, fetch, setInterval, setTimeout,
    clearTimeout, (m) => { confirms.push(m); return env.confirmAnswer; }, PageURL, WebSocket, Blob);
  await settle();
  return env;
}

const screen = { index: 0, output: "HDMI-A-1", ready: true, paused: false, current: 0, temp: null,
  interval: 25, sites: ["a"], urls: ["https://a.example"] };
const st = (host, s = {}) => ({ host, peers: [], screens: [{ ...screen, ...s }] });
const hub = {
  "/api/state": [200, JSON_TYPE, { hub: true, host: "server", peers: [], pis: [
    { host: "kleros1", base: "/api/pi/kleros1", online: true },
    { host: "kleros2", base: "/api/pi/kleros2", online: false }] }],
  "/api/pi/kleros1/api/state": [200, JSON_TYPE, { host: "kleros1", peers: [], screens: [screen] }],
  "/api/pi/kleros2/api/state": [503, JSON_TYPE, { error: "kleros2 is not connected" }],
};
const pi = {
  "/api/state": [200, JSON_TYPE, { host: "kleros1", peers: ["https://kleros1.tn.ts.net", "https://kleros2.tn.ts.net"], screens: [screen] }],
  "https://kleros2.tn.ts.net/api/state": [200, JSON_TYPE, { host: "kleros2", peers: [], screens: [screen] }],
};
const nginx = (status) => [status, HTML_TYPE, `<html><body><h1>${status}</h1><hr>nginx</body></html>`];
const TEAM = "https://mgr.example/?lab_token=T%26x#c=link-code-0123456789";
const PI = "https://kleros1.tn.ts.net/";
const frame = (host, i, bytes) => Uint8Array.from([host.length, ...Buffer.from(host), i, ...bytes]);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const BROWSE = [["Back", "back"], ["Forward", "forward"], ["Reload", "reload"], ["Scroll up", "scroll", { dy: -0.5 }],
  ["Scroll down", "scroll", { dy: 0.5 }]];

const cases = [
  ["hub: both Pis listed, token on every fetch, #c= dropped but ?lab_token kept", TEAM, hub, (r) =>
    r.rendered.join("|") === "kleros1|kleros2|kleros2 is not connected"
    && r.fetched.every((f) => f.includes("lab_token=T%26x")) && r.replaced[0] === "/?lab_token=T%26x"
    && !r.fetched.some((f) => f.includes("ts.net")) && r.els.conn.textContent === "Updating every 3 s"],
  ["Pi mode: Funnel peers fetched without any token, no WebSocket", PI, pi, (r) =>
    r.rendered.join("|") === "kleros1|kleros2" && r.fetched.every((f) => !f.includes("lab_token"))
    && r.sockets.length === 0],
  ["edge1 403 (HTML, bad link token): code kept, no login box, clear message", TEAM,
    { ...hub, "/api/state": nginx(403) }, (r) =>
    r.store.kioskCode === CODE && r.els.login.hidden && has(r.els.toast, "show")
    && r.els.toast.textContent === "Can't reach this Pi: the link was rejected, open the team link again"],
  ["app 403 (JSON, wrong code): code cleared, login box says Wrong access code", TEAM,
    { ...hub, "/api/state": [403, JSON_TYPE, { error: "wrong access code" }] }, (r) =>
    r.store.kioskCode === undefined && !r.els.login.hidden && r.els.loginErr.textContent === "Wrong access code"],
  ["edge1 502 (HTML): status shown instead of a JSON parse error", TEAM,
    { ...hub, "/api/state": nginx(502) }, (r) =>
    r.els.toast.textContent === "Can't reach this Pi: 502 Bad Gateway" && r.store.kioskCode === CODE
    && r.els.conn.textContent === "Reconnecting…"],
  ["edge1 504 (HTML) on one Pi: that Pi shows not connected, the other renders", TEAM,
    { ...hub, "/api/pi/kleros1/api/state": nginx(504) }, (r) =>
    r.rendered.join("|") === "kleros1|kleros1 is not connected|kleros2|kleros2 is not connected"],

  ["login: the code typed in is saved and the page loads", PI, pi, async (r) => {
    const before = !r.els.login.hidden && r.fetched.length === 0;
    r.els.code.value = " typed-code-0123456789 ";
    r.els.login.on.submit({ preventDefault() {} });
    await r.settle();
    return before && r.store.kioskCode === "typed-code-0123456789" && r.els.login.hidden && !r.els.logout.hidden
      && r.rendered.join("|") === "kleros1|kleros2";
  }, { store: {} }],
  ["hub: WebSocket on /ws/phone with the token, auth first, cards from pushes, no polling while live", TEAM, hub, async (r) => {
    const ws = r.ws;
    const url = ws.url === "wss://mgr.example/ws/phone?lab_token=T%26x" && !ws.url.includes(CODE);
    const two = st("kleros1", { sites: ["a", "b"], urls: ["https://a.example", "https://b.example"], current: 1 });
    await r.live({ kleros1: two, kleros2: null });
    ws.push({ type: "hello", pis: [{ host: "kleros1", online: true }, { host: "kleros2", online: false }, { host: "kleros3", online: true }] });
    ws.push({ type: "state", host: "kleros1", online: true, state: two });
    await r.settle();
    const n = r.fetched.length;
    await r.tick(10000);
    const on = r.find((x) => x.className === "site on");
    return url && same(ws.sent[0], { type: "auth", code: CODE }) && r.els.conn.textContent === "Live"
      && r.rendered.join("|") === "kleros1|kleros2|kleros2 is not connected|kleros3|Waiting for kleros3…"
      && on && text(on).startsWith("bOn now") && r.fetched.length === n && r.sockets.length === 1;
  }],
  ["hub: an HTTP poll still running when the WebSocket connects doesn't draw over the pushed state", TEAM, hub, async (r) => {
    let open;
    r.gate = new Promise((go) => { open = go; });
    await r.tick(3000);  // the poll is waiting for its answer
    await r.live({ kleros1: st("kleros1", { paused: true }), kleros2: null });
    const n = r.fetched.length;
    r.gate = null;
    open();
    await r.settle();
    return text(r.find((x) => has(x, "pill"))) === "Paused" && r.els.conn.textContent === "Live"
      && !r.fetched.slice(n).some((f) => f.includes("shot.jpg"));
  }],
  ["hub WS: commands carry an id, the button is busy until the result, results update the card, errors toast", TEAM, hub, async (r) => {
    const two = { sites: ["a", "b"], urls: ["https://a.example", "https://b.example"] };
    const ws = await r.live({ kleros1: st("kleros1", two) });
    const next = r.btn("Next");
    next.onclick();
    next.onclick();  // a second tap while busy is ignored
    const busy = has(next, "busy") && next.attrs["aria-busy"] === "true";
    const [cmd] = r.commands();
    ws.push({ type: "result", id: cmd.id, status: 200, body: st("kleros1", { ...two, paused: true }) });
    await r.settle();
    const updated = !has(next, "busy") && !next.attrs["aria-busy"] && text(r.find((x) => has(x, "pill"))) === "Paused"
      && r.btn("Resume") && !r.btn("Pause");
    r.btn("b").onclick();
    const jump = r.commands()[1];
    ws.push({ type: "result", id: jump.id, status: 409, body: { error: "a video is playing" } });
    await r.settle();
    return busy && updated && r.commands().length === 2 && typeof cmd.id === "string" && cmd.id && jump.id !== cmd.id
      && same({ ...cmd, id: 0 }, { type: "command", id: 0, host: "kleros1", path: "/api/screen/0/next", query: "", body: null })
      && jump.path === "/api/screen/0/jump" && jump.query === "site=1"
      && r.els.toast.textContent === "kleros1: a video is playing" && has(r.els.toast, "show") && r.posts.length === 0;
  }],
  ["hub WS: close 4003 forgets the code, shows the login box and stops reconnecting", TEAM, hub, async (r) => {
    const ws = await r.live({ kleros1: st("kleros1") });
    ws.push({ type: "error", error: "wrong access code" });
    ws.shut(4003);
    await r.settle();
    const n = r.fetched.length;
    await r.tick(60000);
    return r.store.kioskCode === undefined && !r.els.login.hidden && r.els.loginErr.textContent === "Wrong access code"
      && r.rendered.length === 0 && r.sockets.length === 1 && r.fetched.length === n && !has(r.els.toast, "show")
      && r.els.conn.hidden && r.els.logout.hidden;
  }],
  ["hub: a WebSocket that never connects falls back to polling, retries after 1, 2, 4 … 30 s, commands over HTTP", TEAM, hub, async (r) => {
    await r.tick(120000);
    const gaps = r.sockets.slice(1).map((s, i) => s.created - r.sockets[i].created);
    const polls = r.fetched.filter((f) => f.startsWith("/api/state")).length;
    r.btn("Next").onclick();
    await r.settle();
    return same(gaps, [1000, 2000, 4000, 8000, 16000, 30000, 30000]) && polls === 41
      && r.els.conn.textContent === "Updating every 3 s" && r.commands().length === 0
      && same(r.posts.map((p) => [p.url, p.body, p.code]), [["/api/pi/kleros1/api/screen/0/next?lab_token=T%26x", undefined, CODE]]);
  }, { failWs: true }],
  ["hub WS: a dropped connection fails the waiting command, polls at once and reconnects after 1 s", TEAM, hub, async (r) => {
    const ws = await r.live({ kleros1: st("kleros1") });
    r.btn("Next").onclick();
    const n = r.fetched.length;
    ws.shut(1006);
    await r.settle();
    const polled = r.fetched.length > n && r.els.conn.textContent === "Updating every 3 s"
      && r.els.toast.textContent === "kleros1: connection lost, check the TV before trying again" && !has(r.btn("Next"), "busy");
    await r.tick(1000);
    const again = r.sockets.length === 2;
    await r.live({ kleros1: st("kleros1") });
    return polled && again && same(r.sockets[1].sent[0], { type: "auth", code: CODE }) && r.els.conn.textContent === "Live";
  }],
  ["hub WS: binary preview frames set the picture and revoke the previous object URL", TEAM, hub, async (r) => {
    const ws = await r.live({ kleros1: st("kleros1"), kleros2: null });
    const img = r.find((x) => x.tag === "img");
    ws.push(frame("kleros1", 0, [0xff, 0xd8, 0xff, 0xd9]));
    const first = img.src;
    ws.push(frame("kleros1", 0, [0xff, 0xd8, 1, 0xff, 0xd9]));
    ws.push(frame("kleros9", 0, [0xff, 0xd8, 0xff, 0xd9]));
    ws.push(frame("kleros1", 3, [0xff, 0xd8, 0xff, 0xd9]));
    return first.startsWith("blob:") && img.src !== first && img.src.startsWith("blob:") && same(r.revoked, [first])
      && r.blobs.length === 2 && r.blobs[0].size === 4 && r.blobs[0].type === "image/jpeg" && r.blobs[1].size === 5;
  }],
  ["hub WS: watch the TVs on the page, off while the page is hidden, on again when visible", TEAM, hub, async (r) => {
    const ws = await r.live({ kleros1: st("kleros1"), kleros2: null });
    const watches = () => ws.sent.filter((m) => m.type === "watch").map((m) => `${m.host}/${m.screen}/${m.on}`);
    const first = same(watches(), ["kleros1/0/true"]);
    r.setHidden(true);
    const hidden = same(watches(), ["kleros1/0/true", "kleros1/0/false"]);
    r.setHidden(false);
    ws.push({ type: "state", host: "kleros2", online: true, state: st("kleros2") });
    ws.push({ type: "state", host: "kleros1", online: false, state: null });
    return first && hidden && same(watches(), ["kleros1/0/true", "kleros1/0/false", "kleros1/0/true", "kleros2/0/true", "kleros1/0/false"])
      && ws.sent.filter((m) => m.type === "watch").every((m) => typeof m.screen === "number");
  }],
  ["tap on the preview clicks at that spot (fractions of the picture, letterbox ignored, ripple), Pi mode and hub", PI, pi, async (r) => {
    const img = r.find((x) => x.tag === "img"), shot = img.parent;
    const tapAt = (x, y, w, h) => { Object.assign(img, { naturalWidth: w, naturalHeight: h }); img.onclick({ clientX: x, clientY: y }); };
    const before = (tapAt(100, 156.25, 0, 0), r.posts.length === 0);  // no picture yet
    img.rect = { left: 0, top: 100, width: 400, height: 225 };
    tapAt(100, 156.25, 800, 450);
    const ripple = shot.children.some((x) => has(x, "ripple") && x.style.left === "100px" && x.style.top === "56.25px");
    await r.settle();
    tapAt(125, 212.5, 640, 480);  // 4:3 picture: 300 px wide, 50 px bars each side
    await r.settle();
    tapAt(20, 150, 640, 480);     // on the black bar
    await r.tick(700);
    const pi = same(r.posts.map((p) => [p.url, p.body]), [["/api/screen/0/click", { x: 0.25, y: 0.25 }], ["/api/screen/0/click", { x: 0.25, y: 0.5 }]])
      && !shot.children.some((x) => has(x, "ripple"));

    const h = await boot(TEAM, hub);
    const ws = await h.live({ kleros1: st("kleros1") });
    const himg = h.find((x) => x.tag === "img");
    Object.assign(himg, { rect: { left: 10, top: 0, width: 320, height: 180 }, naturalWidth: 768, naturalHeight: 432 });
    himg.onclick({ clientX: 10 + 320 * 0.8, clientY: 180 * 0.1 });
    const [cmd] = h.commands();
    ws.push({ type: "result", id: cmd.id, status: 200, body: st("kleros1") });
    await h.settle();
    return before && ripple && pi && cmd.path === "/api/screen/0/click" && same(cmd.body, { x: 0.8, y: 0.1 }) && h.posts.length === 0;
  }],
  ["browsing: Back/Forward/Reload/Scroll/Type/Enter send the right actions and bodies, Pi mode over HTTP and hub over WS", PI, pi, async (r) => {
    const want = [...BROWSE.map(([, a, b]) => [`/api/screen/0/${a}`, b]), ["/api/screen/0/type", { text: "hello" }],
      ["/api/screen/0/key", { key: "Enter" }], ["/api/screen/0/type", { text: " two" }]];
    const drive = async (env, after) => {
      for (const [l] of BROWSE) { env.btn(l).onclick(); await after(); }
      const box = env.input("Text to type on kleros1");
      env.btn("Type").onclick();  // empty box: nothing sent
      box.value = "hello";
      env.btn("Type").onclick();
      await after();
      const cleared = box.value === "";
      env.btn("Enter").onclick();
      await after();
      box.value = " two";
      let prevented = false;
      box.onkeydown({ key: "Enter", isComposing: false, preventDefault() { prevented = true; } });
      await after();
      return cleared && prevented;
    };
    const piOk = await drive(r, r.settle);
    const pi = piOk && same(r.posts.map((p) => [p.url, p.body]), want) && r.posts.every((p) => p.code === "saved-code-0123456789");

    const h = await boot(TEAM, hub);
    const ws = await h.live({ kleros1: st("kleros1") });
    const answer = async () => { const c = h.commands().at(-1); ws.push({ type: "result", id: c.id, status: 200, body: st("kleros1") }); await h.settle(); };
    const hubOk = await drive(h, answer);
    return pi && hubOk && h.posts.length === 0
      && same(h.commands().map((c) => [c.host, c.path, c.body]), want.map(([p, b]) => ["kleros1", p, b ?? null]));
  }],
  ["Pi mode: half-typed link and preview survive refreshes; Show now sends and clears it; remove asks first", PI,
    { ...pi, "/api/screen/0/shot.jpg": [200, "image/jpeg", "jpeg"] }, async (r) => {
    const link = r.input("Link for kleros1"), img = r.find((x) => x.tag === "img");
    const firstShot = img.src;
    link.value = "half.exa";
    await r.tick(3000);
    const kept = r.input("Link for kleros1") === link && link.value === "half.exa" && r.find((x) => x.tag === "img") === img
      && firstShot.startsWith("blob:") && img.src !== firstShot && r.revoked.includes(firstShot);
    r.btn("Show now").onclick();
    await r.settle();
    const shown = link.value === "" && same(r.posts.at(-1).body, { url: "half.exa" }) && r.posts.at(-1).url === "/api/screen/0/show";
    r.confirmAnswer = false;
    r.btn("Remove a").onclick();
    const asked = r.confirms[0] === "Remove a from kleros1's rotation?" && r.posts.length === 1;
    r.confirmAnswer = true;
    r.btn("Remove a").onclick();
    await r.settle();
    return kept && shown && asked && same(r.posts.at(-1), { url: "/api/screen/0/remove", body: { url: "https://a.example" }, code: "saved-code-0123456789" });
  }],
  ["paused or showing a link: the orange bar says so and tapping it resumes", PI,
    { ...pi, "https://kleros2.tn.ts.net/api/state": [200, JSON_TYPE, st("kleros2", { paused: true, temp: "x.example" })],
      "/api/state": [200, JSON_TYPE, { ...st("kleros1", { paused: true }), peers: ["https://kleros2.tn.ts.net"] }] }, async (r) => {
    const bar = (host) => all(r.card(host)).find((x) => has(x, "bar"));
    const pill = (host) => text(all(r.card(host)).find((x) => has(x, "pill")));
    const ok = !bar("kleros1").hidden && bar("kleros1").textContent === "Rotation paused. Tap to resume." && pill("kleros1") === "Paused"
      && !bar("kleros2").hidden && bar("kleros2").textContent === "Not saved. Tap to go back to the rotation."
      && pill("kleros2") === "Showing a link";
    bar("kleros2").onclick();
    await r.settle();
    return ok && same(r.posts.map((p) => p.url), ["https://kleros2.tn.ts.net/api/screen/0/resume"]);
  }],
  ["logout forgets the code, closes the WebSocket and stops", TEAM, hub, async (r) => {
    const ws = await r.live({ kleros1: st("kleros1") });
    r.els.logout.onclick();
    const n = r.fetched.length;
    await r.tick(60000);
    return r.store.kioskCode === undefined && !r.els.login.hidden && r.els.loginErr.textContent === "" && ws.closed
      && r.sockets.length === 1 && r.fetched.length === n && r.rendered.length === 0;
  }],
];

(async () => {
  let failed = 0;
  for (const [name, href, routes, ok, opts] of cases) {
    const r = await boot(href, routes, opts);
    let pass;
    try { pass = await ok(r); } catch (e) { pass = false; console.log("      error:", e.stack); }
    failed += !pass;
    console.log(`${pass ? "PASS" : "FAIL"}  ${name}`);
    if (!pass) console.log("      rendered:", r.rendered, "| toast:", r.els.toast.textContent, "| conn:", r.els.conn.textContent,
      "| login:", r.els.loginErr.textContent, "| code:", r.store.kioskCode, "| fetched:", r.fetched,
      "| posts:", JSON.stringify(r.posts), "| ws:", JSON.stringify(r.sockets.map((s) => [s.url, s.created, s.sent])));
  }
  console.log(failed ? `${failed} failed` : "all passed");
  process.exit(failed ? 1 : 0);
})();
