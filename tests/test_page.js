// Runs pi/control.html's script with a stub DOM and fetch.   node tests/test_page.js
const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "pi", "control.html"), "utf8");
const src = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));

const JSON_TYPE = "application/json; charset=utf-8";
const HTML_TYPE = "text/html";

async function run(href, routes) {
  const u = new URL(href);
  const fetched = [], replaced = [];
  const mk = (tag) => ({ tag, children: [], textContent: "", hidden: true, className: "", src: "",
    append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; },
    addEventListener() {}, setAttribute() {} });
  const els = { login: mk("form"), loginErr: mk("p"), err: mk("p"), hosts: mk("div"), code: mk("input") };
  const document = { getElementById: (id) => els[id], createElement: mk, activeElement: null, hidden: false };
  const location = { href: u.href, origin: u.origin, pathname: u.pathname, search: u.search, hash: u.hash };
  const history = { replaceState: (a, b, url) => replaced.push(url) };
  const store = { kioskCode: "saved-code-0123456789" };
  const localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; },
    removeItem: (k) => { delete store[k]; } };
  const fetch = async (url) => {
    fetched.push(url);
    const abs = new URL(url, u.href);
    const r = routes[(abs.origin === u.origin ? "" : abs.origin) + abs.pathname]
      || [404, JSON_TYPE, { error: "not found" }];
    const [status, type, body] = r;
    return { ok: status >= 200 && status < 300, status, statusText: { 403: "Forbidden", 502: "Bad Gateway" }[status] || "",
      headers: { get: (h) => (h.toLowerCase() === "content-type" ? type : null) },
      json: async () => { if (typeof body === "string") throw new SyntaxError("Unexpected token '<'"); return body; },
      blob: async () => "blob" };
  };
  const urlApi = Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL() {} });
  new Function("document", "location", "history", "localStorage", "fetch", "setInterval", "confirm", "URL", src)(
    document, location, history, localStorage, fetch, () => 0, () => true, urlApi);
  await new Promise((r) => setTimeout(r, 100));
  const flat = (n) => [n.tag === "h1" || n.className === "off" ? n.textContent : null,
    ...n.children.flatMap(flat)].filter(Boolean);
  return { fetched, replaced, store, els, rendered: flat(els.hosts) };
}

const screen = { index: 0, output: "HDMI-A-1", ready: true, paused: false, current: 0, temp: null,
  interval: 25, sites: ["a"], urls: ["https://a.example"] };
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

const cases = [
  ["hub: both Pis listed, token on every fetch, #c= dropped but ?lab_token kept", TEAM, hub, (r) =>
    r.rendered.join("|") === "kleros1|kleros2|Not reachable right now"
    && r.fetched.every((f) => f.includes("lab_token=T%26x")) && r.replaced[0] === "/?lab_token=T%26x"
    && !r.fetched.some((f) => f.includes("ts.net"))],
  ["Pi mode: Funnel peers fetched without any token", "https://kleros1.tn.ts.net/", pi, (r) =>
    r.rendered.join("|") === "kleros1|kleros2" && r.fetched.every((f) => !f.includes("lab_token"))],
  ["edge1 403 (HTML, bad link token): code kept, no login box, clear message", TEAM,
    { ...hub, "/api/state": nginx(403) }, (r) =>
    r.store.kioskCode === "link-code-0123456789" && r.els.login.hidden
    && r.els.err.textContent === "Can't reach this Pi: the link was rejected, open the team link again"],
  ["app 403 (JSON, wrong code): code cleared, login box says Wrong access code", TEAM,
    { ...hub, "/api/state": [403, JSON_TYPE, { error: "wrong access code" }] }, (r) =>
    r.store.kioskCode === undefined && !r.els.login.hidden && r.els.loginErr.textContent === "Wrong access code"],
  ["edge1 502 (HTML): status shown instead of a JSON parse error", TEAM,
    { ...hub, "/api/state": nginx(502) }, (r) =>
    r.els.err.textContent === "Can't reach this Pi: 502 Bad Gateway" && r.store.kioskCode === "link-code-0123456789"],
  ["edge1 504 (HTML) on one Pi: that Pi shows Not reachable, the other renders", TEAM,
    { ...hub, "/api/pi/kleros1/api/state": nginx(504) }, (r) =>
    r.rendered.join("|") === "kleros1|Not reachable right now|kleros2|Not reachable right now"],
];

(async () => {
  let failed = 0;
  for (const [name, href, routes, ok] of cases) {
    const r = await run(href, routes);
    const pass = ok(r);
    failed += !pass;
    console.log(`${pass ? "PASS" : "FAIL"}  ${name}`);
    if (!pass) console.log("      rendered:", r.rendered, "| err:", r.els.err.textContent,
      "| login:", r.els.loginErr.textContent, "| code:", r.store.kioskCode, "| fetched:", r.fetched);
  }
  console.log(failed ? `${failed} failed` : "all passed");
  process.exit(failed ? 1 : 0);
})();
