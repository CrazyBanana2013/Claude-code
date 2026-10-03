// Browser-Test für die HUD-Oberfläche web/index.html (Playwright, Chromium) gegen einen echten JARVIS-Server.
// Start über tests/test_ui_browser.py: der startet Fake-WLED/ESPHome + JARVIS und setzt
//   JARVIS_URL, JARVIS_TOKEN, FAKE_URL (+ PLAYWRIGHT_MODULE = Pfad zum playwright-Paket).
// Fake-Steuerung (nur Test): GET/POST FAKE/_test/state, POST /_test/sensors, /_test/fail {"wled": "drop"|503|null}, /_test/reset.
// Sicherheitsnetz: /api/confirm/* wird im Browser immer abgefangen (auf Windows würde der PC sonst wirklich herunterfahren).
// Optional: HUD_SHOTS=<ordner> legt Screenshots ab, HUD_ONLY=<text> führt nur passende Tests aus.
import { createRequire } from "module";
import path from "path";
import assert from "assert/strict";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const BASE = (process.env.JARVIS_URL || "").replace(/\/+$/, "");
const TOKEN = process.env.JARVIS_TOKEN || "";
const FAKE = (process.env.FAKE_URL || "").replace(/\/+$/, "");
const SHOTS = process.env.HUD_SHOTS || "";
const ONLY = process.env.HUD_ONLY || "";
if (!BASE || !TOKEN || !FAKE) {
  console.log("JARVIS_URL, JARVIS_TOKEN und FAKE_URL fehlen – den Test über tests/test_ui_browser.py starten.");
  process.exit(2);
}

const PHONE = { width: 390, height: 844 };
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);
const EFFECTS = ["Solid", "Blink", "Breathe", "Wipe", "Rainbow", "Fire 2012", "Colorloop"]; // wie der Fake
const COLORS = { // RGB laut app/tools/led.py
  rot: [255, 0, 0], orange: [255, 90, 0], gelb: [255, 190, 0], "grün": [0, 255, 0], blau: [0, 0, 255],
  lila: [140, 0, 255], pink: [255, 20, 120], "weiß": [255, 255, 255], "warmweiß": [255, 170, 80],
};
const PANELS = ["licht", "klima", "skripte", "pc", "system", "chat"];

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
let failures = 0;
const jsErrors = [];     // pageerror + console.error (ohne Chromiums Netzwerkzeilen zu HTTP-Fehlerantworten)
const foreign = [];      // Anfragen an nicht-lokale Hosts (externe Ressourcen)
const confirmCalls = []; // POST /api/confirm/* aus dem Sicherheitsnetz – darf nie vorkommen
let current = { name: "", recs: [], contexts: [], pages: [] };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, what, timeout = 8000) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try { last = await fn(); if (last) return last; } catch (e) { last = String(e); }
    await sleep(100);
  }
  throw new Error("Zeitüberschreitung: " + what + " (zuletzt: " + JSON.stringify(last) + ")");
}

// ---------- Fake-Geräte (direkt aus Node, nicht über den Browser) ----------
async function fake(pathname = "/_test/state", body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(FAKE + pathname, opts);
  return res.json();
}
const wled = async () => (await fake()).state;
async function jarvis(tool, args = {}) {
  const res = await fetch(BASE + "/api/tools/" + tool, {
    method: "POST", headers: { Authorization: "Bearer " + TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(args),
  });
  return res.json();
}
function alive(pid) {
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; }
}

// ---------- Browser ----------
async function open({ token = TOKEN, viewport = PHONE, reducedMotion = "no-preference", touch = true, dpr = 2, init = null, goto = true } = {}) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: dpr, hasTouch: touch, reducedMotion, locale: "de-DE" });
  context.setDefaultTimeout(10000);
  current.contexts.push(context);
  if (token) {
    await context.addInitScript((t) => {
      try {
        if (!sessionStorage.getItem("hud-test-seeded")) { localStorage.setItem("jarvis.token", t); sessionStorage.setItem("hud-test-seeded", "1"); }
      } catch { /* egal */ }
    }, token);
  }
  await context.addInitScript(toastRecorder);
  if (init) await context.addInitScript(init);
  // Sicherheitsnetz: eine Bestätigung würde auf Windows den PC wirklich herunterfahren → nie zum Server lassen
  await context.route("**/api/confirm/**", (route) => { confirmCalls.push(current.name + ": " + route.request().url()); return route.abort(); });
  const page = await context.newPage();
  current.pages.push(page);
  // allow: erlaubte HTTP-Fehlerantworten; netFail: Test simuliert Verbindungsabbrüche (Chromium meldet sie in der Konsole)
  const rec = { api: [], httpErrors: [], allow: new Set(), netFail: false };
  current.recs.push(rec);
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (!/^(data|blob|about):$/.test(u.protocol) && !LOCAL_HOSTS.has(u.hostname)) foreign.push(current.name + ": " + r.url());
    if (u.pathname.startsWith("/api/")) {
      rec.api.push({ method: r.method(), path: u.pathname, auth: r.headers()["authorization"] || "", body: r.postData() });
    }
  });
  page.on("response", (r) => { if (r.status() >= 400) rec.httpErrors.push(r.status() + " " + new URL(r.url()).pathname); });
  page.on("pageerror", (e) => jsErrors.push(current.name + ": " + (e.stack || e)));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    // Chromium schreibt zu jeder HTTP-Fehlerantwort eine Konsolenzeile; welche Fehler erlaubt sind, prüft rec.allow.
    if (/^Failed to load resource: the server responded with a status of \d+/.test(m.text())) return;
    if (rec.netFail && /^Failed to load resource: net::ERR_(CONNECTION_REFUSED|FAILED)$/.test(m.text())) return;
    jsErrors.push(current.name + ": console.error " + m.text());
  });
  if (goto) await page.goto(BASE + "/");
  return { context, page, rec };
}

const text = (page, sel) => page.locator(sel).textContent();
const shot = (page, name) => (SHOTS ? page.screenshot({ path: path.join(SHOTS, name + ".png") }) : null);
const toolCalls = (rec, tool) => rec.api.filter((a) => a.path === "/api/tools/" + tool);

async function settled(page) {
  // Beim Start fliegen die Knoten aus dem Kern (bzw. die Bühne weicht dem Desktop-Panel aus): warten, bis alles ruht
  const snap = () => page.evaluate(() => [...document.querySelectorAll("#rig > button")].map((b) => b.style.transform).join("|"));
  await until(async () => { const a = await snap(); await sleep(250); return a === (await snap()); }, "Knoten in Ruhe");
}
async function loaded(page) {
  await page.waitForFunction(() => document.body.classList.contains("ready")
    && [...document.querySelectorAll(".node .s")].every((s) => s.textContent !== "lädt …")
    && document.getElementById("nv-skripte").textContent.includes("AKTIV")
    && document.getElementById("nv-klima").textContent !== "–", null, { timeout: 12000 });
  await settled(page);
}
async function openPanel(page, trigger, id) {
  await page.click(trigger);
  await page.waitForSelector(`#panel-${id}.open:not([hidden])`);
  assert.equal(await page.getAttribute(trigger, "aria-expanded"), "true", trigger + " aria-expanded");
  assert.equal(await page.locator(".panel:not([hidden])").count(), 1, "genau ein Panel offen");
}
const panelClosed = (page, id) => page.waitForSelector(`#panel-${id}`, { state: "hidden" });
// Aktion ausführen und auf die Antwort des Tool-Aufrufs warten
async function call(page, tool, action) {
  const resp = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/tools/" + tool, { timeout: 10000 });
  await action();
  const r = await resp;
  return { status: r.status(), body: await r.json().catch(() => ({})), args: JSON.parse(r.request().postData() || "{}") };
}
async function toastIs(page, re, bad = null) {
  await page.waitForFunction(([src, flags, bad]) => {
    const t = document.getElementById("toast");
    return t.classList.contains("show") && new RegExp(src, flags).test(t.textContent) && (bad === null || t.classList.contains("bad") === bad);
  }, [re.source, re.flags, bad], { timeout: 6000 }).catch(async () => {
    throw new Error("Toast " + re + (bad ? " (Fehler)" : "") + " erwartet, ist: " + JSON.stringify([await text(page, "#toast"), await page.getAttribute("#toast", "class")]));
  });
}
const waitText = (page, sel, expected) => page.waitForFunction(([s, e]) => document.querySelector(s).textContent === e, [sel, expected])
  .catch(async () => { throw new Error(sel + " = " + JSON.stringify(await text(page, sel)) + ", erwartet " + JSON.stringify(expected)); });
const canvasSnap = (page) => page.evaluate(() => document.getElementById("fx").toDataURL());
// Jeder angezeigte Toast landet in window.__toasts ("bad: " vorne = Fehler-Toast)
const toastRecorder = () => {
  window.__toasts = [];
  document.addEventListener("DOMContentLoaded", () => {
    const t = document.getElementById("toast");
    if (!t) return;
    new MutationObserver(() => {
      if (!t.classList.contains("show")) return;
      const entry = (t.classList.contains("bad") ? "bad: " : "") + t.textContent;
      if (window.__toasts[window.__toasts.length - 1] !== entry) window.__toasts.push(entry);
    }).observe(t, { attributes: true, attributeFilter: ["class"], childList: true, characterData: true, subtree: true });
  });
};
const toasts = (page) => page.evaluate(() => window.__toasts);
const json = (status, obj) => ({ status, contentType: "application/json", body: JSON.stringify(obj) });
const rafCounter = () => {
  window.__raf = 0;
  const orig = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (cb) => { window.__raf++; return orig(cb); };
};

async function test(name, fn) {
  if (ONLY && !name.includes(ONLY)) return;
  current = { name, recs: [], contexts: [], pages: [] };
  try {
    await fake("/_test/reset", {});
    await fn();
    for (const rec of current.recs) {
      const unexpected = rec.httpErrors.filter((e) => !rec.allow.has(e) && !e.endsWith(" /favicon.ico"));
      assert.deepEqual(unexpected, [], "unerwartete HTTP-Fehlerantworten");
    }
    console.log("ok   " + name);
  } catch (e) {
    failures++;
    console.log("FAIL " + name + "\n     " + (e.stack || e));
    if (SHOTS) for (const [i, p] of current.pages.entries()) await p.screenshot({ path: path.join(SHOTS, "FAIL-" + name.replace(/\W+/g, "_").slice(0, 40) + "-" + i + ".png") }).catch(() => {});
  } finally {
    for (const c of current.contexts) await c.close().catch(() => {});
  }
}

// =====================================================================

await test("erster Aufruf ohne Token: Dialog, Token wird gespeichert, Status ok", async () => {
  const { page, rec } = await open({ token: null });
  assert.equal(await page.title(), "JARVIS");
  await page.waitForSelector("#token-dialog[open]");
  await waitText(page, "#ns-licht", "Zugang fehlt");
  assert.deepEqual(rec.api.map((a) => a.method + " " + a.path).filter((a) => a !== "GET /api/health"), [], "ohne Token keine Daten-Anfragen");
  await shot(page, "first-open");
  await page.fill("#token-input", TOKEN);
  await page.click("#token-form button[type=submit]");
  await page.waitForFunction(() => !document.getElementById("token-dialog").open);
  assert.equal(await page.evaluate(() => localStorage.getItem("jarvis.token")), TOKEN);
  await loaded(page);
  assert.match(await page.getAttribute("#st-server", "class"), /\bok\b/);
  assert.equal(await text(page, "#st-server"), "Server");
  assert.match(await page.getAttribute("#st-llm", "class"), /\bwarn\b/);
  assert.equal(await text(page, "#st-llm"), "LLM aus · Regeln");
  assert.equal(await text(page, "#nv-system"), "REGELN");
  assert.equal(await text(page, "#nv-pc"), "BEREIT");
  assert.equal(await text(page, "#nv-licht"), "AUS");
  assert.equal(await text(page, "#nv-klima"), "21,4 °C");
  assert.equal(await text(page, "#nv-skripte"), "0 AKTIV");
  const authed = rec.api.filter((a) => a.path !== "/api/health");
  assert.ok(authed.length >= 4 && authed.every((a) => a.auth === "Bearer " + TOKEN), "Bearer-Token bei jeder API-Anfrage");
  await page.reload();
  await loaded(page);
  assert.equal(await page.evaluate(() => document.getElementById("token-dialog").open), false, "nach Neuladen kein Dialog");
});

await test("Token-Dialog weggedrückt: die nächste Aktion fragt erneut, ohne Anfrage zu senden", async () => {
  const { page, rec } = await open({ token: null });
  await page.waitForSelector("#token-dialog[open]");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.getElementById("token-dialog").open);
  await openPanel(page, "#n-licht", "licht");
  await page.click("#led-on");
  await page.waitForSelector("#token-dialog[open]");
  assert.equal(toolCalls(rec, "led_power").length, 0);
});

await test("jeder Knoten öffnet sein Panel; Schließen per Knopf, Escape und Tipp daneben", async () => {
  const { page } = await open();
  await loaded(page);
  await shot(page, "m390-idle");
  const triggers = [["#n-licht", "licht"], ["#n-klima", "klima"], ["#n-skripte", "skripte"], ["#n-pc", "pc"],
    ["#n-system", "system"], ["#core", "chat"], ["#cmd", "chat"], ["#status-btn", "system"]];
  for (const [trigger, id] of triggers) {
    await openPanel(page, trigger, id);
    assert.ok(await page.evaluate((id) => document.getElementById("panel-" + id).contains(document.activeElement), id), id + ": Fokus im Panel");
    if (SHOTS && trigger.startsWith("#n-")) await shot(page, "m390-" + id);
    await page.click(`#panel-${id} [data-close]`);
    await panelClosed(page, id);
    assert.equal(await page.getAttribute(trigger, "aria-expanded"), "false");
    assert.ok(await page.evaluate((t) => document.activeElement === document.querySelector(t), trigger), trigger + ": Fokus zurück am Auslöser");
    await openPanel(page, trigger, id);
    await page.keyboard.press("Escape");
    await panelClosed(page, id);
  }
  // direkt umschalten: die obere Knotenreihe bleibt über dem Sheet antippbar
  await openPanel(page, "#n-licht", "licht");
  await openPanel(page, "#n-klima", "klima");
  assert.ok(await page.locator("#panel-licht").isHidden(), "Licht-Panel zu, wenn Klima aufgeht");
  assert.equal(await page.getAttribute("#n-licht", "aria-expanded"), "false");
  // gleicher Knoten nochmal = zu (aber erst nach der Doppeltipp-Sperre von 350 ms)
  await sleep(400);
  await page.click("#n-klima");
  await panelClosed(page, "klima");
  // Doppeltipp auf einen Knoten (~0,2 s Abstand, wie ein echter Finger): Panel bleibt offen
  const ring = await page.locator("#n-licht .ring").boundingBox();
  await page.touchscreen.tap(ring.x + ring.width / 2, ring.y + ring.height / 2);
  await sleep(80);
  await page.touchscreen.tap(ring.x + ring.width / 2, ring.y + ring.height / 2);
  await sleep(450);
  assert.ok(await page.locator("#panel-licht.open:not([hidden])").isVisible(), "Doppeltipp lässt das Licht-Panel offen");
  await page.keyboard.press("Escape");
  await panelClosed(page, "licht");
  // Tipp auf freie HUD-Fläche schließt
  await openPanel(page, "#n-pc", "pc");
  const free = await page.evaluate(() => {
    for (let y = 80; y < innerHeight * 0.5; y += 12) for (let x = 20; x < innerWidth - 20; x += 12) {
      if (document.elementFromPoint(x, y)?.id === "fx") return { x, y };
    }
    return null;
  });
  assert.ok(free, "freie Fläche gefunden");
  await page.mouse.click(free.x, free.y);
  await panelClosed(page, "pc");
});

await test("schnell schließen und gleich ein anderes Panel öffnen: das alte wird trotzdem verborgen", async () => {
  for (const [viewport, touch, dpr] of [[PHONE, true, 2], [{ width: 1280, height: 800 }, false, 1]]) {
    const { page, context } = await open({ viewport, touch, dpr });
    await loaded(page);
    for (const close of ["Escape", "x"]) {
      await openPanel(page, "#n-licht", "licht");
      if (close === "Escape") await page.keyboard.press("Escape"); else await page.click("#panel-licht [data-close]");
      // sofort, noch während das Licht-Panel ausblendet. Am Desktop gleitet die Bühne dabei zurück (Knoten bewegt sich),
      // daher dort per Ereignis statt per Mausposition auslösen.
      if (touch) await page.click("#n-skripte", { force: true });
      else await page.locator("#n-skripte").dispatchEvent("click");
      await page.waitForSelector("#panel-skripte.open:not([hidden])").catch(async (e) => {
        const st = await page.evaluate(() => ({ panels: [...document.querySelectorAll(".panel")].map((p) => p.id + (p.hidden ? ":hidden" : "") + (p.classList.contains("open") ? ":open" : "")),
          exp: document.getElementById("n-skripte").getAttribute("aria-expanded"), inert: document.getElementById("n-skripte").inert, body: document.body.className }));
        throw new Error(viewport.width + " " + close + ": Skripte-Panel öffnet nicht " + JSON.stringify(st) + "\n" + e.message);
      });
      await sleep(600);
      const st = await page.evaluate(() => [...document.querySelectorAll(".panel")].filter((p) => !p.hidden).map((p) => p.id));
      assert.deepEqual(st, ["panel-skripte"], viewport.width + " " + close + ": nur das neue Panel ist offen (das alte nicht per Tab erreichbar)");
      await page.keyboard.press("Escape");
      await panelClosed(page, "skripte");
    }
    await context.close();
  }
});

await test("Licht: An/Aus, Helligkeit, Farben, Effekt, Preset ändern den WLED-Zustand", async () => {
  const { page, rec } = await open();
  await loaded(page);
  await openPanel(page, "#n-licht", "licht");
  await waitText(page, "#led-state", "aus");

  let r = await call(page, "led_power", () => page.click("#led-on"));
  assert.deepEqual(r.args, { state: "on" });
  assert.equal((await wled()).on, true);
  await waitText(page, "#led-state", "an · 50 %");
  assert.equal(await page.getAttribute("#led-on", "aria-pressed"), "true");
  r = await call(page, "led_power", () => page.click("#led-off"));
  assert.equal((await wled()).on, false);
  await waitText(page, "#led-state", "aus");
  await waitText(page, "#nv-licht", "AUS");
  assert.equal(await page.getAttribute("#led-off", "aria-pressed"), "true");
  assert.ok(await page.evaluate(() => document.activeElement.id === "led-off"), "Fokus bleibt auf dem Knopf");
  await call(page, "led_power", () => page.click("#led-on"));
  await waitText(page, "#nv-licht", "AN · 50 %");

  // Helligkeit: Ziehen löst viele input-Events aus, aber genau eine Anfrage (beim Loslassen)
  const slider = page.locator("#bri");
  await slider.scrollIntoViewIfNeeded();
  const box = await slider.boundingBox();
  const xAt = (pct) => box.x + 14 + (box.width - 28) * pct / 100; // Daumen 28 px
  const y = box.y + box.height / 2;
  const before = toolCalls(rec, "led_brightness").length;
  const resp = page.waitForResponse((x) => x.url().endsWith("/api/tools/led_brightness"));
  await page.mouse.move(xAt(Number(await slider.inputValue())), y);
  await page.mouse.down();
  await page.mouse.move(xAt(45), y, { steps: 3 });
  assert.equal(toolCalls(rec, "led_brightness").length, before, "während des Ziehens keine Anfrage");
  assert.match(await text(page, "#bri-val"), /^\d+ %$/);
  await page.mouse.move(xAt(30), y, { steps: 6 });
  await page.mouse.up();
  const bresp = await resp;
  await sleep(400);
  assert.equal(toolCalls(rec, "led_brightness").length, before + 1, "genau ein led_brightness pro Änderung");
  const pct = JSON.parse(bresp.request().postData()).percent;
  assert.ok(pct >= 22 && pct <= 38, "Zielwert ~30 %: " + pct);
  const bri = (await wled()).bri;
  assert.ok(Math.abs(bri - pct * 2.55) <= 0.5, `WLED bri ${bri} passt zu ${pct} %`);
  await waitText(page, "#led-state", "an · " + Math.round(bri * 100 / 255) + " %");

  // alle neun Farbfelder
  for (const [name, rgb] of Object.entries(COLORS)) {
    r = await call(page, "led_color", () => page.click(`[aria-label="Farbe ${name}"]`));
    assert.deepEqual(r.args, { color: name });
    assert.equal(r.status, 200, name);
    assert.deepEqual((await wled()).seg[0].col[0], rgb, "WLED-Farbe für " + name);
    await page.waitForFunction((n) => document.querySelector(`[aria-label="Farbe ${n}"]`).getAttribute("aria-pressed") === "true"
      && document.getElementById("ns-licht").textContent === n, name);
  }
  assert.equal(await page.locator('.swatch[aria-pressed="true"]').count(), 1, "nur ein Farbfeld markiert");
  // freie Farbe
  r = await call(page, "led_color", () => page.evaluate(() => {
    const c = document.querySelector("#swatches input[type=color]");
    c.value = "#12ab34";
    c.dispatchEvent(new Event("input", { bubbles: true }));
    c.dispatchEvent(new Event("change", { bubbles: true }));
  }));
  assert.deepEqual(r.args, { color: "#12ab34" });
  assert.deepEqual((await wled()).seg[0].col[0], [18, 171, 52]);
  await waitText(page, "#ns-licht", "#12ab34");
  await shot(page, "m390-licht-done");

  // Effekte: Chip, freier Name, unbekannter Name
  r = await call(page, "led_effect", () => page.click('#fx-chips .chip[data-v="Rainbow"]'));
  assert.equal((await wled()).seg[0].fx, EFFECTS.indexOf("Rainbow"));
  await toastIs(page, /Effekt „Rainbow“ aktiv/, false);
  assert.match(await page.getAttribute('#fx-chips .chip[data-v="Rainbow"]', "class"), /\bsel\b/);
  await page.fill("#fx-input", "breathe");
  r = await call(page, "led_effect", () => page.press("#fx-input", "Enter"));
  assert.deepEqual(r.args, { effect: "breathe" });
  assert.equal((await wled()).seg[0].fx, EFFECTS.indexOf("Breathe"));
  await toastIs(page, /Effekt „Breathe“ aktiv/, false);
  await page.fill("#fx-input", "Gibtsnicht");
  rec.allow.add("400 /api/tools/led_effect");
  r = await call(page, "led_effect", () => page.press("#fx-input", "Enter"));
  assert.equal(r.status, 400);
  await toastIs(page, /Effekt 'Gibtsnicht' nicht gefunden/, true);
  assert.equal((await wled()).seg[0].fx, EFFECTS.indexOf("Breathe"), "unbekannter Effekt ändert nichts");

  // Presets: Chip (Nummer) und freier Name
  r = await call(page, "led_preset", () => page.click('#ps-chips .chip[data-v="2"]'));
  assert.deepEqual(r.args, { preset: "2" });
  assert.equal((await wled()).ps, 2);
  await toastIs(page, /Preset „2“ geladen/, false);
  await page.fill("#ps-input", "Abend");
  r = await call(page, "led_preset", () => page.press("#ps-input", "Enter"));
  assert.equal((await wled()).ps, 1);
  await toastIs(page, /Preset „Abend“ geladen/, false);

  // Zustand neu lesen (am Gerät geändert)
  await fake("/_test/state", { on: true, bri: 255 });
  await call(page, "led_status", () => page.click("#led-refresh"));
  await waitText(page, "#led-state", "an · 100 %");
  await waitText(page, "#nv-licht", "AN · 100 %");
  // Payloads an WLED wie laut Doku: "v": true, "seg" als Objekt
  const posts = (await fake()).posts;
  assert.deepEqual(posts[0], { on: true, v: true });
  assert.ok(posts.some((p) => JSON.stringify(p) === JSON.stringify({ on: true, seg: { col: [[255, 0, 0]] }, v: true })), "Farbe rot als seg-Objekt");
});

await test("Klima: Werte mit Dezimalkomma, Zeitstempel, Fehler pro Sensor", async () => {
  const { page } = await open();
  await loaded(page);
  assert.equal(await text(page, "#nv-klima"), "21,4 °C");
  assert.equal(await text(page, "#ns-klima"), "47,2 %");
  await openPanel(page, "#n-klima", "klima");
  await page.waitForFunction(() => /21,4 °C/.test(document.getElementById("sensors").textContent) && /47,2 %/.test(document.getElementById("sensors").textContent));
  assert.match(await text(page, "#sensor-time"), /^Stand: \d\d:\d\d:\d\d$/);
  assert.deepEqual(await page.locator("#sensors .sensor .name").allTextContents(), ["Temperatur", "Luftfeuchte"]);
  await fake("/_test/sensors", { Temp: 19.06, Hum: null }); // Luftfeuchte-Sensor liefert HTTP 500
  await call(page, "sensors_read", () => page.click("#sensor-refresh"));
  await page.waitForFunction(() => /19,1 °C/.test(document.getElementById("sensors").textContent));
  assert.equal(await text(page, "#sensors .sensor:nth-child(2) .err"), "HTTP 500");
  await waitText(page, "#nv-klima", "19,1 °C");
  await waitText(page, "#ns-klima", "Luftfeuchte: –");
  await shot(page, "m390-klima-error");
});

await test("Skripte: Dummy starten (PID) und stoppen, CS2 nicht eingerichtet", async () => {
  const { page, rec } = await open();
  let pid = null;
  try {
    await loaded(page);
    await openPanel(page, "#n-skripte", "skripte");
    const label = (name) => page.locator("#scripts .script", { hasText: name }).locator(".label").textContent();
    await until(async () => (await label("Dummy-Skript")) === "gestoppt", "Dummy gestoppt");
    assert.equal(await label("CS2-Skript"), "nicht eingerichtet (TODO)");
    assert.equal(await text(page, "#skripte-state"), "0 von 2 aktiv");

    let r = await call(page, "scripts_start", () => page.click('button[aria-label="Starten: Dummy-Skript"]'));
    assert.equal(r.status, 200);
    assert.deepEqual(r.args, { script_id: "dummy" });
    pid = r.body.result.pid;
    const shown = await until(async () => { const t = await label("Dummy-Skript"); return /^läuft · PID \d+$/.test(t) && t; }, "Dummy läuft");
    assert.equal(shown, "läuft · PID " + pid);
    assert.ok(alive(pid), "Dummy-Prozess läuft wirklich");
    await waitText(page, "#nv-skripte", "1 AKTIV");
    assert.equal(await text(page, "#skripte-state"), "1 von 2 aktiv");
    await shot(page, "m390-skripte-running");

    r = await call(page, "scripts_stop", () => page.click('button[aria-label="Stoppen: Dummy-Skript"]'));
    assert.equal(r.status, 200);
    assert.ok(["stopped", "killed"].includes(r.body.result.status), r.body.result.status);
    await until(async () => (await label("Dummy-Skript")) === "gestoppt", "Dummy wieder gestoppt");
    await until(() => !alive(pid), "Dummy-Prozess beendet", 10000);
    await waitText(page, "#nv-skripte", "0 AKTIV");
    pid = null;

    rec.allow.add("400 /api/tools/scripts_start");
    r = await call(page, "scripts_start", () => page.click('button[aria-label="Starten: CS2-Skript"]'));
    assert.equal(r.status, 400);
    await toastIs(page, /CS2-Skript.*noch nicht eingerichtet/, true);
    assert.equal(await label("CS2-Skript"), "nicht eingerichtet (TODO)");
  } finally {
    if (pid) await jarvis("scripts_stop", { script_id: "dummy" }).catch(() => {});
  }
});

await test("Befehl: Regel-Parser-Antworten mit Tool-Chips, Zustände werden nachgeführt", async () => {
  await fake("/_test/state", { on: true });
  const { page } = await open();
  try {
    await loaded(page);
    await openPanel(page, "#cmd", "chat");
    assert.equal(await page.getAttribute("#chat-input", "maxlength"), "500");
    const bots = page.locator("#log .msg.bot");
    async function say(msg) {
      const n = await bots.count();
      await page.fill("#chat-input", msg);
      const resp = page.waitForResponse((r) => r.url().endsWith("/api/chat"));
      await page.press("#chat-input", "Enter");
      const r = await resp;
      assert.deepEqual(JSON.parse(r.request().postData()), { message: msg });
      await until(async () => (await bots.count()) === n + 1, "Antwort auf " + msg);
      assert.equal(await page.locator("#log .msg.me").last().textContent(), msg);
      assert.equal(await page.inputValue("#chat-input"), "", "Eingabe geleert");
      return bots.last();
    }
    let last = await say("Licht aus");
    assert.match(await last.textContent(), /Licht aus\./);
    assert.deepEqual(await last.locator(".tc").allTextContents(), ["✓ led_power"]);
    assert.match(await last.locator(".tc").getAttribute("class"), /\bok\b/);
    assert.match(await last.locator(".meta").textContent(), /Regel-Parser · Kein Modell konfiguriert \(llm\.model ist TODO\)\./);
    assert.equal((await wled()).on, false);
    await waitText(page, "#nv-licht", "AUS");
    await waitText(page, "#cmd-text", "Licht aus.");

    // Regel-Parser: "auf 30 %" = led_brightness (schaltet selbst ein), "rot" = led_color – kein eigenes led_power
    last = await say("Licht auf 30 % und rot");
    assert.deepEqual(await last.locator(".tc").allTextContents(), ["✓ led_brightness", "✓ led_color"]);
    assert.match(await last.textContent(), /Helligkeit 30 %\. Farbe rot\./);
    const s = await wled();
    assert.equal(s.on, true);
    assert.deepEqual(s.seg[0].col[0], [255, 0, 0]);
    await waitText(page, "#nv-licht", "AN · 30 %");
    await waitText(page, "#ns-licht", "rot");

    last = await say("wie warm ist es");
    assert.match(await last.textContent(), /Temperatur: 21,4 °C/);
    assert.deepEqual(await last.locator(".tc").allTextContents(), ["✓ sensors_read"]);

    last = await say("starte cs2");
    assert.deepEqual(await last.locator(".tc").allTextContents(), ["✗ scripts_start"]);
    assert.match(await last.locator(".tc").getAttribute("class"), /\bbad\b/);
    assert.match(await last.textContent(), /noch nicht eingerichtet/);

    last = await say("starte dummy");
    assert.deepEqual(await last.locator(".tc").allTextContents(), ["✓ scripts_start"]);
    await waitText(page, "#nv-skripte", "1 AKTIV");
    last = await say("stoppe dummy");
    await waitText(page, "#nv-skripte", "0 AKTIV");

    // Vorschlags-Chips: am Handy bei offener Tastatur (Eingabe fokussiert) ausgeblendet, danach antippbar
    assert.ok(await page.evaluate(() => document.activeElement.id === "chat-input"));
    assert.ok(await page.locator("#suggest").isHidden(), "Vorschläge bei offener Tastatur ausgeblendet");
    await page.evaluate(() => document.activeElement.blur()); // Tastatur zu
    await page.waitForSelector("#suggest .chip >> nth=0", { state: "visible" });
    const n = await bots.count();
    await page.click('#suggest .chip:text-is("Licht an")');
    await until(async () => (await bots.count()) === n + 1, "Antwort auf Vorschlag");
    assert.equal((await wled()).on, true);

    // 500 Zeichen ohne Leerzeichen: Eingabe begrenzt, Protokoll bricht um statt seitlich zu scrollen
    await page.fill("#chat-input", "x".repeat(495));
    await page.locator("#chat-input").press("End");
    await page.locator("#chat-input").pressSequentially("x".repeat(15)); // echte Tastendrücke über die Grenze
    assert.equal((await page.inputValue("#chat-input")).length, 500, "maxlength 500");
    await page.press("#chat-input", "Enter");
    await until(async () => (await bots.count()) === n + 2, "Antwort auf langen Text");
    const log = await page.evaluate(() => { const l = document.getElementById("log"); return [l.scrollWidth, l.clientWidth]; });
    assert.ok(log[0] <= log[1], "Protokoll ohne horizontales Scrollen " + log);
    await shot(page, "m390-chat");
  } finally {
    await jarvis("scripts_stop", { script_id: "dummy" }).catch(() => {});
  }
});

await test("Herunterfahren: Dialog mit Countdown, Abbrechen schickt keine Bestätigung", async () => {
  const { page, rec } = await open();
  await loaded(page);
  await openPanel(page, "#n-pc", "pc");
  assert.match(await text(page, "#panel-pc .warnbox"), /15 s/);
  assert.equal(await text(page, "#pc-state"), "Kein Herunterfahren geplant.");
  const r = await call(page, "pc_shutdown", () => page.click("#shutdown"));
  assert.equal(r.body.result.status, "confirm_required");
  await page.waitForSelector("#confirm-dialog[open]");
  assert.ok(await page.evaluate(() => document.activeElement.id === "confirm-no"), "Fokus auf Abbrechen");
  assert.match(await text(page, "#confirm-text"), /15 s/);
  const n1 = Number(await text(page, "#confirm-count"));
  assert.ok(n1 <= r.body.result.expires_in && n1 >= r.body.result.expires_in - 1, "Start bei expires_in: " + n1);
  await sleep(2300);
  const n2 = Number(await text(page, "#confirm-count"));
  assert.ok(n2 <= n1 - 2 && n2 >= n1 - 3, `Countdown läuft: ${n1} → ${n2}`);
  await shot(page, "m390-confirm");
  await page.click("#confirm-no");
  await page.waitForFunction(() => !document.getElementById("confirm-dialog").open);
  await sleep(1500);
  assert.equal(await page.evaluate(() => document.getElementById("confirm-dialog").open), false);
  assert.ok(!/abgelaufen/.test(await text(page, "#toast")), "kein Ablauf-Toast nach Abbrechen");
  assert.equal(rec.api.filter((a) => a.path.startsWith("/api/confirm")).length, 0, "keine /api/confirm-Anfrage");

  // auch per Chat: confirm_required öffnet den Dialog, Escape schließt ihn
  await page.keyboard.press("Escape");
  await panelClosed(page, "pc");
  await openPanel(page, "#cmd", "chat");
  await page.fill("#chat-input", "PC herunterfahren");
  await page.press("#chat-input", "Enter");
  await page.waitForSelector("#confirm-dialog[open]");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.getElementById("confirm-dialog").open);
  assert.ok(await page.locator("#panel-chat").isVisible(), "Escape schließt nur den Dialog, nicht das Panel");
  await page.click("#panel-chat [data-close]");
  await panelClosed(page, "chat");

  // Ablauf: kurze Gültigkeit → Dialog schließt sich selbst
  await page.route("**/api/tools/pc_shutdown", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ ok: true, tool: "pc_shutdown", result: { status: "confirm_required", confirm_id: "abgelaufen-1", expires_in: 2 } }),
  }));
  await openPanel(page, "#n-pc", "pc");
  await page.click("#shutdown");
  await page.waitForSelector("#confirm-dialog[open]");
  assert.equal(await text(page, "#confirm-count"), "2");
  await page.waitForFunction(() => !document.getElementById("confirm-dialog").open, null, { timeout: 4000 });
  await toastIs(page, /Bestätigung abgelaufen/);
  assert.equal(await page.evaluate(() => document.activeElement.id), "shutdown", "Fokus nach Ablauf zurück auf „Herunterfahren“");
  await page.unroute("**/api/tools/pc_shutdown");

  // "Abbrechen" im Panel = pc_shutdown_cancel (auf Windows gemockt: kein echtes "shutdown /a" im Test)
  if (process.platform === "win32") {
    await page.route("**/api/tools/pc_shutdown_cancel", (route) => route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ ok: true, tool: "pc_shutdown_cancel", result: { status: "nothing_to_cancel", message: "Es war kein Herunterfahren geplant." } }) }));
    await call(page, "pc_shutdown_cancel", () => page.click("#shutdown-cancel"));
    await toastIs(page, /kein Herunterfahren geplant/, false);
  } else {
    rec.allow.add("400 /api/tools/pc_shutdown_cancel");
    const c = await call(page, "pc_shutdown_cancel", () => page.click("#shutdown-cancel"));
    assert.equal(c.status, 400);
    await toastIs(page, /nur auf dem Windows-PC verfügbar/, true);
  }
  assert.equal(rec.api.filter((a) => a.path.startsWith("/api/confirm")).length, 0, "keine /api/confirm-Anfrage");
});

await test("Bestätigen (gemockt, erreicht den Server nie): richtige confirm-ID, PC-Countdown", async () => {
  const { page, rec } = await open({ goto: false });
  const confirmed = [];
  await page.route("**/api/tools/pc_shutdown", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ ok: true, tool: "pc_shutdown", result: { status: "confirm_required", confirm_id: "id-123_x", expires_in: 30 } }),
  }));
  await page.route("**/api/confirm/**", (route) => {
    confirmed.push(route.request().method() + " " + new URL(route.request().url()).pathname);
    return route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ ok: true, result: { status: "shutdown_scheduled", message: "PC fährt in 15 Sekunden herunter." } }) });
  });
  await page.goto(BASE + "/");
  await loaded(page);
  await openPanel(page, "#n-pc", "pc");
  await page.click("#shutdown");
  await page.waitForSelector("#confirm-dialog[open]");
  await page.click("#confirm-yes");
  await page.waitForFunction(() => !document.getElementById("confirm-dialog").open);
  await toastIs(page, /PC fährt in 15 Sekunden herunter/);
  assert.deepEqual(confirmed, ["POST /api/confirm/id-123_x"]);
  await page.waitForFunction(() => /^AUS IN 1[45] s$/.test(document.getElementById("nv-pc").textContent));
  assert.match(await text(page, "#pc-state"), /PC fährt in 1[45] s herunter\./);
  assert.ok(rec.api.every((a) => !a.path.startsWith("/api/confirm") || confirmed.length === 1));
});

await test("401: falscher Token → Token-Dialog mit Hinweis, genau ein Fehlversuch; auch mitten in der Sitzung", async () => {
  const wrong = "falsch-" + "0".repeat(40);
  const { page, rec } = await open({ token: wrong });
  rec.allow.add("401 /api/status");
  await page.waitForSelector("#token-dialog[open]");
  await page.waitForFunction(() => !document.getElementById("token-err").hidden);
  assert.equal(await text(page, "#token-err"), "Token ungültig. Bitte erneut eingeben.");
  assert.equal(await page.evaluate(() => localStorage.getItem("jarvis.token")), null, "falscher Token gelöscht");
  assert.equal(rec.api.filter((a) => a.auth === "Bearer " + wrong).length, 1, "nur eine Anfrage mit falschem Token");
  await shot(page, "m390-badtoken");
  await page.fill("#token-input", TOKEN);
  await page.click("#token-form button[type=submit]");
  await page.waitForFunction(() => !document.getElementById("token-dialog").open);
  await loaded(page);
  assert.ok(await page.locator("#token-err").isHidden());

  // Token wird mitten in der Sitzung abgelehnt (gemockt)
  await page.route("**/api/tools/scripts_list", (route) => route.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"Token ungültig."}' }), { times: 1 });
  rec.allow.add("401 /api/tools/scripts_list");
  await openPanel(page, "#n-skripte", "skripte");
  await page.waitForSelector("#token-dialog[open]");
  assert.equal(await page.evaluate(() => localStorage.getItem("jarvis.token")), null);
  await page.fill("#token-input", TOKEN);
  await page.click("#token-form button[type=submit]");
  await page.waitForFunction(() => !document.getElementById("token-dialog").open);
  await until(async () => (await page.locator("#scripts .script").count()) === 2, "Skripte nach erneuter Anmeldung");
  assert.equal(await page.evaluate(() => localStorage.getItem("jarvis.token")), TOKEN);
});

await test("429 (gemockt): Sperrtext als Toast, Knoten 'gesperrt', danach Status prüfen lädt alles", async () => {
  const { page, rec } = await open({ goto: false });
  await page.route("**/api/status", (route) => route.fulfill({ status: 429, contentType: "application/json", body: '{"detail":"Zu viele Fehlversuche. Gesperrt für 42 s."}' }), { times: 1 });
  rec.allow.add("429 /api/status");
  await page.goto(BASE + "/");
  await toastIs(page, /Zu viele Fehlversuche\. Gesperrt für 42 s\./, true);
  await waitText(page, "#ns-licht", "gesperrt");
  assert.equal(toolCalls(rec, "led_status").length, 0, "keine Tool-Aufrufe während der Sperre");
  await openPanel(page, "#n-system", "system");
  await page.click("#sys-refresh");
  await toastIs(page, /Status aktualisiert/, false);
  await loaded(page);
});

await test("System: Diagnose-Werte und 'Token vergessen'", async () => {
  const { page } = await open();
  await loaded(page);
  await openPanel(page, "#n-system", "system");
  await waitText(page, "#sys-server", "erreichbar");
  await waitText(page, "#sys-llm", "LLM aus · Regeln");
  assert.match(await text(page, "#sys-lat"), /^\d+ ms$/);
  assert.match(await text(page, "#sys-note"), /Kein Modell konfiguriert/);
  await page.click("#logout");
  await page.waitForSelector("#token-dialog[open]");
  assert.equal(await page.evaluate(() => localStorage.getItem("jarvis.token")), null);
  assert.ok(await page.locator("#panel-system").isHidden() || !(await page.locator("#panel-system").evaluate((p) => p.classList.contains("open"))));
  await page.fill("#token-input", TOKEN);
  await page.press("#token-input", "Enter");
  await page.waitForFunction(() => !document.getElementById("token-dialog").open);
  assert.equal(await page.evaluate(() => localStorage.getItem("jarvis.token")), TOKEN);
  await loaded(page);
});

await test("360×740: kein horizontales Scrollen, alles im Bild, Tap-Ziele ≥ 44 px", async () => {
  const { page } = await open({ viewport: { width: 360, height: 740 } });
  await loaded(page);
  await shot(page, "m360-idle");
  const check = async (where) => {
    const o = await page.evaluate(() => ({ sw: document.scrollingElement.scrollWidth, iw: innerWidth, dw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
    assert.ok(o.sw <= o.iw && o.dw <= o.cw, where + ": horizontales Scrollen " + JSON.stringify(o));
  };
  await check("Ruhezustand");
  const out = await page.evaluate(() => {
    const bad = [];
    for (const e of document.querySelectorAll("#rig .node .ring, #rig .node .v, #rig .node .t, #cmd, #status-btn, .brand")) {
      const r = e.getBoundingClientRect();
      if (r.left < -0.5 || r.right > innerWidth + 0.5 || r.top < -0.5 || r.bottom > innerHeight + 0.5) bad.push((e.closest("[id]")?.id || "") + " " + e.className + " " + JSON.stringify([r.left, r.right]));
    }
    for (const b of document.querySelectorAll("#rig > button, #cmd, #status-btn")) {
      const r = b.getBoundingClientRect();
      if (r.width < 44 || r.height < 44) bad.push(b.id + " " + Math.round(r.width) + "x" + Math.round(r.height));
    }
    return bad;
  });
  assert.deepEqual(out, [], "Knoten/Leisten außerhalb oder zu klein");
  for (const id of PANELS) {
    const trigger = id === "chat" ? "#cmd" : "#n-" + id;
    await openPanel(page, trigger, id);
    await sleep(350);
    if (id === "licht" || id === "chat") await shot(page, "m360-" + id);
    await check(id);
    const g = await page.evaluate((id) => {
      const p = document.getElementById("panel-" + id), b = p.querySelector(".p-body"), r = p.getBoundingClientRect();
      const small = [];
      for (const e of p.querySelectorAll("button, input:not([type=color]), label.swatch")) {
        const q = e.getBoundingClientRect();
        if (q.width && (q.width < 43.5 || q.height < 43.5)) small.push((e.id || e.getAttribute("aria-label") || e.textContent) + " " + Math.round(q.width) + "x" + Math.round(q.height));
      }
      return { l: r.left, r: r.right, sw: b.scrollWidth, cw: b.clientWidth, small };
    }, id);
    assert.ok(g.l >= 0 && g.r <= 360.5, id + ": Panel passt in die Breite " + JSON.stringify(g));
    assert.ok(g.sw <= g.cw, id + ": Panel ohne horizontales Scrollen " + JSON.stringify(g));
    assert.deepEqual(g.small, [], id + ": Bedienelemente < 44 px");
    await page.keyboard.press("Escape");
    await panelClosed(page, id);
  }
});

await test("Desktop 1280×800: Seitenpanel verdeckt Kern und Knoten nicht, '/' öffnet den Befehl", async () => {
  const { page } = await open({ viewport: { width: 1280, height: 800 }, touch: false, dpr: 1 });
  await loaded(page);
  assert.ok(await page.evaluate(() => document.body.classList.contains("m-dock")));
  await shot(page, "d-idle");
  await openPanel(page, "#n-licht", "licht");
  await settled(page);
  await sleep(400);
  await shot(page, "d-licht");
  const g = await page.evaluate(() => {
    const p = document.getElementById("panel-licht").getBoundingClientRect();
    const items = [...document.querySelectorAll("#core, #rig .node .ring")].map((e) => [e.closest("button").id, e.getBoundingClientRect().right]);
    return { left: p.left, items };
  });
  for (const [id, right] of g.items) assert.ok(right <= g.left, `${id} vom Panel verdeckt (${right} > ${g.left})`);
  await page.keyboard.press("Escape");
  await panelClosed(page, "licht");
  await settled(page);
  await page.keyboard.press("/");
  await page.waitForSelector("#panel-chat.open:not([hidden])");
  assert.ok(await page.evaluate(() => document.activeElement.id === "chat-input"), "Fokus im Eingabefeld");
  await page.keyboard.type("Licht an");
  await page.keyboard.press("Enter");
  await until(async () => (await wled()).on === true, "Licht an per Tastatur");
  await page.keyboard.press("Escape");
  await panelClosed(page, "chat");
  // Tastatur: Knoten per Fokus + Enter öffnen
  await page.focus("#n-klima");
  await page.keyboard.press("Enter");
  await page.waitForSelector("#panel-klima.open:not([hidden])");
  await page.keyboard.press("Escape");
  await panelClosed(page, "klima");
  assert.ok(await page.evaluate(() => document.activeElement.id === "n-klima"), "Fokus zurück am Knoten");
});

await test("Animation: Canvas bewegt sich, DPR ≤ 2, Schleife pausiert bei verborgenem Tab", async () => {
  const { page } = await open({ dpr: 3, init: rafCounter });
  await loaded(page);
  const dims = await page.evaluate(() => ({ cw: document.getElementById("fx").width, iw: innerWidth }));
  assert.ok(dims.cw <= dims.iw * 2 + 1, "Canvas-Auflösung auf DPR 2 begrenzt " + JSON.stringify(dims));
  const a = await canvasSnap(page);
  await sleep(300);
  assert.notEqual(await canvasSnap(page), a, "Strudel animiert");
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await sleep(200);
  const r0 = await page.evaluate(() => window.__raf);
  await sleep(700);
  const r1 = await page.evaluate(() => window.__raf);
  assert.ok(r1 - r0 <= 1, "keine Bilder bei verborgenem Tab: " + (r1 - r0));
  await page.evaluate(() => {
    delete document.hidden; delete document.visibilityState;
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await sleep(700);
  assert.ok((await page.evaluate(() => window.__raf)) - r1 > 10, "Animation läuft wieder");
});

await test("prefers-reduced-motion: ruhiges Standbild ohne Dauerschleife, alles bedienbar", async () => {
  const { page } = await open({ reducedMotion: "reduce", init: rafCounter });
  await loaded(page);
  assert.ok(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches));
  const lit = await page.evaluate(() => {
    const c = document.getElementById("fx"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 4 * 7) if (d[i] + d[i + 1] + d[i + 2] > 120) n++;
    return n;
  });
  assert.ok(lit > 500, "HUD ist gezeichnet (helle Pixel: " + lit + ")");
  await shot(page, "m390-reduced");
  const a = await canvasSnap(page);
  const r0 = await page.evaluate(() => window.__raf);
  await sleep(1200);
  assert.equal(await canvasSnap(page), a, "Standbild bleibt ruhig");
  assert.ok((await page.evaluate(() => window.__raf)) - r0 <= 2, "keine rAF-Dauerschleife");
  assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector(".ring .spin")).animationName), "none");
  await openPanel(page, "#n-licht", "licht");
  await call(page, "led_power", () => page.click("#led-on"));
  await waitText(page, "#nv-licht", "AN · 50 %");
  await page.keyboard.press("Escape");
  await panelClosed(page, "licht");
});

await test("Status wird alle 30 s neu gelesen, Sensoren/Skripte still alle 60 s", async () => {
  const { page, rec } = await open({ goto: false });
  await page.clock.install();
  await page.goto(BASE + "/");
  await until(async () => { await page.clock.runFor(200); return toolCalls(rec, "scripts_list").length >= 1 && toolCalls(rec, "sensors_read").length >= 1; }, "Startladung");
  const count = (p) => rec.api.filter((a) => a.path === p).length;
  const h0 = count("/api/health"), s0 = count("/api/status"), sl0 = toolCalls(rec, "scripts_list").length, sr0 = toolCalls(rec, "sensors_read").length;
  await page.clock.fastForward(30000);
  await until(() => count("/api/health") === h0 + 1 && count("/api/status") === s0 + 1, "Status nach 30 s");
  await page.clock.fastForward(30000);
  await until(() => toolCalls(rec, "scripts_list").length === sl0 + 1 && toolCalls(rec, "sensors_read").length === sr0 + 1, "stilles Nachladen nach 60 s");
  assert.equal(count("/api/health"), h0 + 2, "zweiter Status nach 60 s");
});

await test("Licht: WLED nicht erreichbar – stiller Start ohne Toast, Meldung als Zustand, Aktion meldet den Fehler", async () => {
  await fake("/_test/fail", { wled: "drop" }); // Gerät legt ohne Antwort auf
  const { page, rec } = await open();
  rec.allow.add("400 /api/tools/led_status");
  rec.allow.add("400 /api/tools/led_power");
  await loaded(page);
  await waitText(page, "#nv-licht", "–");
  await waitText(page, "#ns-licht", "nicht erreichbar");
  assert.match(await text(page, "#led-state"), /^WLED nicht erreichbar \(\w+\)\.$/);
  await openPanel(page, "#n-licht", "licht");
  await until(() => toolCalls(rec, "led_status").length >= 2, "Licht-Panel liest den Zustand neu");
  await sleep(400);
  assert.deepEqual(await toasts(page), [], "kein Toast beim stillen Lesen");
  await shot(page, "m390-licht-unreachable");
  const r = await call(page, "led_power", () => page.click("#led-on"));
  assert.equal(r.status, 400);
  await toastIs(page, /^WLED nicht erreichbar \(\w+\)\.$/, true);
  // Gerät wieder da → "Neu lesen" zeigt den echten Zustand
  await fake("/_test/fail", { wled: null });
  await call(page, "led_status", () => page.click("#led-refresh"));
  await waitText(page, "#led-state", "aus");
  await waitText(page, "#nv-licht", "AUS");
  await waitText(page, "#ns-licht", "");
});

await test("Licht: WLED nicht eingerichtet (gemockt) – voller Hinweis als Zustand, kein Toast, Panel ohne Querscrollen", async () => {
  const msg = "WLED ist noch nicht eingerichtet: 'wled.base_url' in config.yaml eintragen.";
  const { page, rec } = await open({ goto: false });
  await page.route("**/api/tools/led_status", (route) => route.fulfill(json(400, { ok: false, error: msg })));
  rec.allow.add("400 /api/tools/led_status");
  await page.goto(BASE + "/");
  await loaded(page);
  await waitText(page, "#ns-licht", "nicht eingerichtet");
  assert.equal(await text(page, "#nv-licht"), "–");
  assert.equal(await text(page, "#led-state"), msg);
  await openPanel(page, "#n-licht", "licht");
  await until(() => toolCalls(rec, "led_status").length >= 2, "Licht-Panel liest den Zustand neu");
  await sleep(400);
  assert.deepEqual(await toasts(page), [], "kein Toast");
  const g = await page.evaluate(() => { const b = document.querySelector("#panel-licht .p-body"), s = document.getElementById("led-state").getBoundingClientRect();
    return { sw: b.scrollWidth, cw: b.clientWidth, right: s.right, vw: innerWidth, h: s.height }; });
  assert.ok(g.sw <= g.cw && g.right <= g.vw, "Hinweis passt in die Breite " + JSON.stringify(g));
  assert.ok(g.h > 0, "Hinweis sichtbar");
  await shot(page, "m390-licht-todo");
});

await test("Fehlerantworten (gemockt): 404/422 als Fehler-Toast mit Servertext, Chat-Fehler und LLM-Antwort", async () => {
  const { page, rec } = await open();
  await loaded(page);
  await openPanel(page, "#n-licht", "licht");
  await page.route("**/api/tools/led_color", (route) => route.fulfill(json(404, { ok: false, error: "Unbekanntes Tool: led_color" })), { times: 1 });
  rec.allow.add("404 /api/tools/led_color");
  await page.click('[aria-label="Farbe blau"]');
  await toastIs(page, /^Unbekanntes Tool: led_color$/, true);
  await page.route("**/api/tools/led_preset", (route) => route.fulfill(json(422, { ok: false, error: "Ungültige Argumente für led_preset: preset zu lang" })), { times: 1 });
  rec.allow.add("422 /api/tools/led_preset");
  await page.fill("#ps-input", "Abend");
  await page.press("#ps-input", "Enter");
  await toastIs(page, /^Ungültige Argumente für led_preset: preset zu lang$/, true);
  // FastAPI-Validierungsfehler ohne "error" (detail als Liste) → "HTTP 422"
  await page.route("**/api/tools/led_effect", (route) => route.fulfill(json(422, { detail: [{ loc: ["body"], msg: "x" }] })), { times: 1 });
  rec.allow.add("422 /api/tools/led_effect");
  await page.click('#fx-chips .chip[data-v="Solid"]');
  await toastIs(page, /^HTTP 422$/, true);
  assert.equal((await wled()).seg[0].col[0].join(), "0,200,255", "gemockte Fehler ändern das Gerät nicht");
  await page.keyboard.press("Escape");
  await panelClosed(page, "licht");

  await openPanel(page, "#cmd", "chat");
  const bots = page.locator("#log .msg.bot");
  await page.route("**/api/chat", (route) => route.fulfill({ status: 500, contentType: "text/plain", body: "Internal Server Error" }), { times: 1 });
  rec.allow.add("500 /api/chat");
  let n = await bots.count();
  await page.fill("#chat-input", "Licht an");
  await page.press("#chat-input", "Enter");
  await until(async () => (await bots.count()) === n + 1, "Fehlerantwort im Protokoll");
  assert.match(await bots.last().getAttribute("class"), /\berr\b/);
  assert.equal(await bots.last().textContent(), "HTTP 500");
  await waitText(page, "#cmd-text", "HTTP 500");
  assert.equal((await wled()).on, false);

  // LLM-Antwort: Quelle "LLM", ✓/✗-Chips, Licht-Zustand aus dem Ergebnis übernommen
  await page.route("**/api/chat", (route) => route.fulfill(json(200, {
    reply: "Licht ist rot.", source: "llm",
    tool_calls: [{ tool: "led_color", ok: true, result: { on: true, brightness_percent: 64, color: [255, 0, 0], effect_id: 0, preset_id: -1 } },
      { tool: "scripts_start", ok: false, error: "Skript 'CS2-Skript' ist noch nicht eingerichtet." }],
  })), { times: 1 });
  n = await bots.count();
  await page.fill("#chat-input", "mach rot und starte cs2");
  await page.press("#chat-input", "Enter");
  await until(async () => (await bots.count()) === n + 1, "LLM-Antwort");
  const last = bots.last();
  assert.deepEqual(await last.locator(".tc").allTextContents(), ["✓ led_color", "✗ scripts_start"]);
  assert.equal(await last.locator(".meta > span:not(.tc)").textContent(), "LLM");
  await waitText(page, "#nv-licht", "AN · 64 %");
  await waitText(page, "#ns-licht", "rot");
});

await test("Status-Varianten (gemockt): LLM-Pille wie bisher, Server offline färbt das HUD rot, danach wieder ok", async () => {
  const { page, rec } = await open();
  await loaded(page);
  let llm = null;
  await page.route("**/api/status", (route) => (llm ? route.fulfill(json(200, { server: "ok", llm })) : route.continue()));
  await openPanel(page, "#n-system", "system");
  const cases = [
    [{ enabled: false, model: null, reachable: false, note: "LLM deaktiviert – Regel-Parser aktiv." }, "warn", "LLM aus · Regeln", "REGELN", "Regeln"],
    [{ enabled: true, model: "qwen2.5:7b", reachable: false, model_installed: null, tools_supported: null }, "bad", "LLM offline", "LLM OFFLINE", "Regeln"],
    [{ enabled: true, model: "qwen2.5:7b", reachable: true, model_installed: false, tools_supported: null }, "bad", "Modell fehlt", "KEIN MODELL", "Regeln"],
    [{ enabled: true, model: "gemma2", reachable: true, model_installed: true, tools_supported: false }, "bad", "Modell ohne Tools", "OHNE TOOLS", "Regeln"],
    [{ enabled: true, model: "qwen2.5:7b", reachable: true, model_installed: true, tools_supported: true }, "ok", "LLM qwen2.5:7b", "LLM AKTIV", "LLM"],
  ];
  for (const [l, cls, pill, node, src] of cases) {
    llm = l;
    await page.click("#sys-refresh");
    await waitText(page, "#st-llm", pill);
    assert.match(await page.getAttribute("#st-llm", "class"), new RegExp("\\b" + cls + "\\b"), pill);
    await waitText(page, "#nv-system", node);
    assert.equal(await text(page, "#sys-llm"), pill);
    assert.equal(await text(page, "#cmd-src"), src);
  }
  assert.equal(await text(page, "#sys-model"), "qwen2.5:7b");
  await shot(page, "m390-system-llm");

  // Server weg: Pille "Server offline", HUD im Alarmzustand, Fehler-Toast
  rec.netFail = true;
  await page.route("**/api/health", (route) => route.abort("connectionrefused"));
  await page.click("#sys-refresh");
  await waitText(page, "#st-server", "Server offline");
  assert.match(await page.getAttribute("#st-server", "class"), /\bbad\b/);
  await toastIs(page, /^Server nicht erreichbar$/, true);
  assert.ok(await page.evaluate(() => document.body.classList.contains("alert")), "HUD im Alarmzustand");
  await waitText(page, "#nv-system", "OFFLINE");
  await waitText(page, "#nv-pc", "OFFLINE");
  await page.keyboard.press("Escape");
  await panelClosed(page, "system");
  await sleep(400);
  await shot(page, "m390-offline");
  // Aktion bei Server weg: alle Tool-Anfragen scheitern → Fehler-Toast
  await page.route("**/api/tools/**", (route) => route.abort("connectionrefused"));
  await openPanel(page, "#n-skripte", "skripte");
  await page.click('button[aria-label="Starten: Dummy-Skript"]');
  await toastIs(page, /^Server nicht erreichbar$/, true);
  await page.unroute("**/api/tools/**");
  await page.keyboard.press("Escape");
  await panelClosed(page, "skripte");
  // Server wieder da
  await page.unroute("**/api/health");
  llm = null;
  await openPanel(page, "#n-system", "system");
  await waitText(page, "#st-server", "Server");
  assert.match(await page.getAttribute("#st-server", "class"), /\bok\b/);
  assert.ok(await page.evaluate(() => !document.body.classList.contains("alert")), "Alarm wieder aus");
  await waitText(page, "#nv-pc", "BEREIT");
  await waitText(page, "#st-llm", "LLM aus · Regeln");
  assert.equal(toolCalls(rec, "scripts_start").length, 1, "nur der eine (abgebrochene) Startversuch");
});

await test("Knoten-Anzeigen: längste Status-Texte passen ohne Abschneiden und ohne Überlappung (hoch und quer)", async () => {
  // Texte, die die Oberfläche wirklich setzt (setNode): je Knoten [Wert, Unterzeile]
  const sets = [
    { licht: ["–", "nicht eingerichtet"], klima: ["-12,5 °C", "Luftfeuchte: –"], skripte: ["10 AKTIV", "nicht erreichbar"],
      pc: ["AUS IN 15 s", "Abbrechen möglich"], system: ["LLM OFFLINE", "Regel-Parser aktiv"] },
    { licht: ["AN · 100 %", "warmweiß"], klima: ["100,0 %", "Zugang fehlt"], skripte: ["0 AKTIV", "2 Skripte"],
      pc: ["OFFLINE", "keine Verbindung"], system: ["KEIN MODELL", "keine Verbindung"] },
    { licht: ["–", "nicht erreichbar"], klima: ["21,4 °C", "47,2 %"], skripte: ["–", "nicht eingerichtet"],
      pc: ["BEREIT", "mit Bestätigung"], system: ["OHNE TOOLS", "Regel-Parser aktiv"] },
  ];
  for (const viewport of [{ width: 360, height: 740 }, { width: 390, height: 844 }, { width: 844, height: 390 }, { width: 740, height: 360 },
    { width: 740, height: 304 }, { width: 568, height: 270 }]) {
    const { page, context } = await open({ viewport });
    await loaded(page);
    for (const set of sets) {
      const bad = await page.evaluate((set) => {
        for (const [id, [v, sub]] of Object.entries(set)) {
          document.getElementById("nv-" + id).textContent = v;
          document.getElementById("ns-" + id).textContent = sub;
        }
        const out = [];
        for (const e of document.querySelectorAll("#rig .node .v, #rig .node .s")) {
          if (e.scrollWidth > e.clientWidth + 0.5 || e.scrollHeight > e.clientHeight + 1) out.push(e.id + " abgeschnitten: " + JSON.stringify(e.textContent));
          const r = e.getBoundingClientRect();
          if (r.left < 0 || r.right > innerWidth) out.push(e.id + " außerhalb");
        }
        const hit = (a, b) => a.width && b.width && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
        const parts = [...document.querySelectorAll("#rig .node")].map((n) => [n.id, [...n.querySelectorAll(".t, .v, .s, .ring")].map((e) => e.getBoundingClientRect())]);
        const fixed = ["bar", "cmd", "core"].map((id) => [id, document.getElementById(id).getBoundingClientRect()]);
        for (const [id, rs] of parts) for (const r of rs) {
          for (const [oid, o] of fixed) if (hit(r, o)) out.push(id + " überlappt " + oid);
          for (const [id2, rs2] of parts) if (id2 !== id && rs2.some((r2) => hit(r, r2))) out.push(id + " überlappt " + id2);
        }
        return [...new Set(out)];
      }, set);
      assert.deepEqual(bad, [], viewport.width + "×" + viewport.height + " " + JSON.stringify(set));
    }
    if (SHOTS) await shot(page, "long-" + viewport.width + "x" + viewport.height);
    await context.close();
  }
});

await test("Handy quer 844×390: kein Querscrollen, alle Knoten erreichbar, Panel wechselt direkt", async () => {
  const { page } = await open({ viewport: { width: 844, height: 390 } });
  await loaded(page);
  await shot(page, "l844-idle");
  const inView = () => page.evaluate(() => {
    const bad = [];
    for (const e of document.querySelectorAll("#rig > button, #core, #cmd")) {
      const r = e.getBoundingClientRect();
      if (r.left < -0.5 || r.right > innerWidth + 0.5 || r.top < -0.5 || r.bottom > innerHeight + 0.5) bad.push(e.id + " " + JSON.stringify([r.left, r.top, r.right, r.bottom]));
    }
    return { bad, sw: document.scrollingElement.scrollWidth, iw: innerWidth };
  });
  let v = await inView();
  assert.deepEqual(v.bad, [], "außerhalb des Bildes");
  assert.ok(v.sw <= v.iw, "horizontales Scrollen " + JSON.stringify(v));
  await openPanel(page, "#n-licht", "licht");
  await sleep(350);
  await shot(page, "l844-licht");
  const g = await page.evaluate(() => {
    const p = document.getElementById("panel-licht").getBoundingClientRect(), b = document.querySelector("#panel-licht .p-body");
    return { l: p.left, r: p.right, t: p.top, bt: p.bottom, sw: b.scrollWidth, cw: b.clientWidth, iw: innerWidth, ih: innerHeight };
  });
  assert.ok(g.l >= 0 && g.r <= g.iw + 0.5 && g.t >= 0 && g.bt <= g.ih + 0.5, "Panel im Bild " + JSON.stringify(g));
  assert.ok(g.sw <= g.cw, "Panel ohne Querscrollen " + JSON.stringify(g));
  v = await inView();
  assert.ok(v.sw <= v.iw, "horizontales Scrollen mit Panel " + JSON.stringify(v));
  for (const [trigger, id] of [["#n-skripte", "skripte"], ["#n-pc", "pc"], ["#n-system", "system"], ["#n-klima", "klima"]]) {
    await openPanel(page, trigger, id); // jeder Knoten bleibt neben dem Panel antippbar
  }
  await page.keyboard.press("Escape");
  await panelClosed(page, "klima");
});

await test("Handy quer flach (568×270, 740×304 – Browserleiste sichtbar): kein Knoten unter der Befehlsleiste, Panels breit genug", async () => {
  for (const viewport of [{ width: 568, height: 270 }, { width: 740, height: 304 }]) {
    const { page, context } = await open({ viewport });
    await loaded(page);
    const bad = await page.evaluate(() => {
      const out = [];
      const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
      const cmd = document.getElementById("cmd").getBoundingClientRect();
      for (const n of document.querySelectorAll("#rig .node")) {
        for (const e of n.querySelectorAll(".ring, .t, .v, .s")) {
          const r = e.getBoundingClientRect();
          if (!r.width) continue;
          if (hit(r, cmd)) out.push(n.id + " unter der Befehlsleiste");
          if (r.left < 0 || r.right > innerWidth || r.top < 0 || r.bottom > innerHeight) out.push(n.id + " außerhalb");
        }
        const rr = n.querySelector(".ring").getBoundingClientRect();
        const t = document.elementFromPoint(rr.left + rr.width / 2, rr.top + rr.height / 2);
        if (!n.contains(t)) out.push(n.id + " nicht antippbar (verdeckt von " + (t && (t.id || t.className)) + ")");
        if (rr.width < 44 || rr.height < 44) out.push(n.id + " Ring < 44 px");
      }
      const c = cmd;
      if (c.width < 150 || c.height < 44) out.push("Befehlsleiste zu klein " + Math.round(c.width) + "x" + Math.round(c.height));
      if (document.scrollingElement.scrollWidth > innerWidth) out.push("Querscrollen");
      return [...new Set(out)];
    });
    assert.deepEqual(bad, [], viewport.width + "×" + viewport.height);
    await shot(page, "flat-" + viewport.width + "x" + viewport.height);
    for (const id of ["licht", "skripte", "pc", "system", "klima"]) {
      await openPanel(page, "#n-" + id, id);
      await sleep(400);
      const g = await page.evaluate((id) => {
        const p = document.getElementById("panel-" + id), b = p.querySelector(".p-body"), r = p.getBoundingClientRect();
        const x = p.querySelector(".x").getBoundingClientRect();
        const covered = [...document.querySelectorAll("#rig .node, #status-btn")].filter((n) => {
          const q = (n.querySelector(".ring") || n).getBoundingClientRect(), cx = q.left + q.width / 2, cy = q.top + q.height / 2;
          return cx > r.left && cx < r.right && cy > r.top && cy < r.bottom && !n.inert;
        }).map((n) => n.id);
        return { l: r.left, r: r.right, t: r.top, b: r.bottom, w: r.width, sw: b.scrollWidth, cw: b.clientWidth, iw: innerWidth, ih: innerHeight, xt: x.top, covered };
      }, id);
      assert.ok(g.l >= 0 && g.r <= g.iw + 0.5 && g.t >= 0 && g.b <= g.ih + 0.5 && g.xt >= 0, id + ": Panel im Bild " + JSON.stringify(g));
      assert.ok(g.w >= 300, id + ": Panel mindestens 300 px breit " + JSON.stringify(g));
      assert.ok(g.sw <= g.cw, id + ": Panel ohne Querscrollen " + JSON.stringify(g));
      assert.deepEqual(g.covered, [], id + ": verdeckte Knoten müssen inert sein");
      if (id === "licht") await shot(page, "flat-" + viewport.width + "x" + viewport.height + "-licht");
      await page.keyboard.press("Escape");
      await panelClosed(page, id);
    }
    await context.close();
  }
});

await test("lange Listen (gemockt): 8 Skripte mit langen Namen, 6 Sensoren mit langen Fehlern – kein Querscrollen auf 320 px und am Desktop", async () => {
  const scripts = [
    { id: "a", label: "CounterStrike2AutoAcceptMatchmakingBotSkriptVersionZwei", configured: true, running: true, pid: 123456 },
    { id: "b", label: "Nächtliches Backup der Fotosammlung auf das NAS (inkrementell, mit Prüfsummen)", configured: true, running: false, pid: null },
    { id: "c", label: "Übertragung", configured: false, running: false, pid: null },
    { id: "d", label: "Größenänderung-Bilder-Wohnzimmer-Rahmen-Diashow-Generator", configured: true, running: true, pid: 99999999 },
    { id: "e", label: "E", configured: true, running: false, pid: null },
    { id: "f", label: "Spotify-Lautstärke-Ducking während Discord-Anrufen", configured: true, running: false, pid: null },
    { id: "g", label: "ÄÖÜäöüß Umlaut-Test", configured: false, running: false, pid: null },
    { id: "h", label: "Minecraft-Server (Forge 1.20.1, 8 GB RAM, Port 25565)", configured: true, running: true, pid: 4242 },
  ];
  const sensors = [
    { name: "Temperatur", value: null, unit: "°C", timestamp: null, error: "ESPHome antwortet nicht: ConnectTimeout beim Verbinden mit http://192.168.0.123/sensor/temperatur_wohnzimmer_links (nach 3 s)" },
    { name: "Luftfeuchtigkeitssensorwohnzimmer", value: 47.25, unit: "%", timestamp: "2026-10-03T10:00:00+00:00", error: null },
    { name: "CO₂", value: 812.4, unit: "ppm", timestamp: "2026-10-03T10:00:01+00:00", error: null },
    { name: "Außentemperatur Balkon", value: -3.5, unit: "°C", timestamp: "2026-10-03T10:00:02+00:00", error: null },
    { name: "Druck", value: 1013.25, unit: "hPa", timestamp: "2026-10-03T10:00:03+00:00", error: null },
    { name: "Fehlerhaft", value: null, unit: "°C", timestamp: null, error: "noch nicht eingerichtet (base_url/entity_id in config.yaml eintragen)" },
  ];
  for (const [viewport, dpr] of [[{ width: 320, height: 568 }, 2], [{ width: 1920, height: 1080 }, 1]]) {
    const { page, context } = await open({ viewport, dpr, goto: false });
    await page.route("**/api/tools/scripts_list", (r) => r.fulfill(json(200, { ok: true, tool: "scripts_list", result: { scripts } })));
    await page.route("**/api/tools/sensors_read", (r) => r.fulfill(json(200, { ok: true, tool: "sensors_read", result: { sensors } })));
    await page.goto(BASE + "/");
    await loaded(page);
    assert.equal(await text(page, "#nv-skripte"), "3 AKTIV");
    assert.equal(await text(page, "#ns-skripte"), "8 Skripte");
    assert.equal(await text(page, "#nv-klima"), "FEHLER");
    const w = viewport.width + " px";
    const header = await page.evaluate(() => [...document.querySelectorAll(".pill")].filter((e) => e.scrollWidth > e.clientWidth + 0.5).map((e) => e.textContent));
    assert.deepEqual(header, [], w + ": Status-Pillen abgeschnitten");
    for (const id of ["skripte", "klima", "pc", "system", "licht"]) {
      await openPanel(page, "#n-" + id, id);
      await sleep(350);
      const g = await page.evaluate((id) => {
        const p = document.getElementById("panel-" + id), b = p.querySelector(".p-body"), r = p.getBoundingClientRect();
        const out = [...p.querySelectorAll("*")].filter((e) => { const q = e.getBoundingClientRect(); return q.width && (q.right > r.right + 0.5 || q.left < r.left - 0.5); })
          .map((e) => e.tagName + "." + (e.className.baseVal ?? e.className) + " " + JSON.stringify((e.textContent || "").slice(0, 24)));
        return { sw: b.scrollWidth, cw: b.clientWidth, out: out.slice(0, 5), page: document.scrollingElement.scrollWidth <= innerWidth };
      }, id);
      assert.ok(g.sw <= g.cw && g.page, w + " " + id + ": Querscrollen " + JSON.stringify(g));
      assert.deepEqual(g.out, [], w + " " + id + ": Elemente ragen aus dem Panel");
      if (id === "skripte") {
        // letzter Knopf nach Scrollen erreichbar
        const ok = await page.evaluate(() => {
          const body = document.querySelector("#panel-skripte .p-body"); body.scrollTop = body.scrollHeight;
          const b = [...document.querySelectorAll("#scripts button")].pop(), q = b.getBoundingClientRect();
          const t = document.elementFromPoint(q.left + q.width / 2, q.top + q.height / 2);
          return b === t || b.contains(t);
        });
        assert.ok(ok, w + ": letzter Skript-Knopf nicht antippbar");
        await shot(page, "lists-" + viewport.width + "-skripte");
      }
      if (id === "klima") await shot(page, "lists-" + viewport.width + "-klima");
      await page.keyboard.press("Escape");
      await panelClosed(page, id);
    }
    await context.close();
  }
});

await test("gesamt: keine JS-/Konsolenfehler, keine externen Anfragen, nie /api/confirm zum Server", async () => {
  assert.deepEqual(jsErrors, [], "JS-/Konsolenfehler");
  assert.deepEqual(foreign, [], "Anfragen an fremde Hosts");
  assert.deepEqual(confirmCalls, [], "Bestätigung an den Server geschickt");
});

await browser.close();
if (failures) { console.log(failures + " Test(s) fehlgeschlagen"); process.exit(1); }
console.log("alle HUD-Tests bestanden");
