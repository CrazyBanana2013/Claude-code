// Browser-Test für launcher/wake.html (Playwright, Chromium).
// Aufruf: node tests/web/wake.test.mjs   (PLAYWRIGHT_MODULE = Pfad zum playwright-Paket, optional)
import { createRequire } from "module";
import { fileURLToPath, pathToFileURL } from "url";
import path from "path";
import assert from "assert/strict";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const here = path.dirname(fileURLToPath(import.meta.url));
const WAKE = pathToFileURL(path.join(here, "..", "..", "launcher", "wake.html")).href;
const JARVIS = "http://100.64.1.2:8765";
const SETTINGS = { mac: "AABBCCDDEEFF", host: "beispiel.myfritz.net", port: 9, url: JARVIS };

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
let failures = 0;

async function newPage({ settings = null, popupBlocked = false } = {}) {
  const context = await browser.newContext({ viewport: { width: 390, height: 800 } });
  const page = await context.newPage();
  await page.addInitScript(([s, blocked]) => {
    if (s && !localStorage.getItem("jarvis.wake.settings")) localStorage.setItem("jarvis.wake.settings", JSON.stringify(s));
    window.__opened = [];
    window.open = (u) => { window.__opened.push(u); return blocked ? null : {}; };
    window.__fetchOpts = [];
    const realFetch = window.fetch.bind(window);
    window.fetch = (u, opts) => { window.__fetchOpts.push({ mode: opts && opts.mode, signal: !!(opts && opts.signal) }); return realFetch(u, opts); };
  }, [settings, popupBlocked]);
  page.on("pageerror", (e) => { throw e; });
  return { context, page };
}

async function test(name, fn) {
  try { await fn(); console.log("ok   " + name); }
  catch (e) { failures++; console.log("FAIL " + name + "\n     " + (e.stack || e)); }
}

await test("ohne Einstellungen öffnet sich der Dialog, ungültige Werte werden abgelehnt", async () => {
  const { context, page } = await newPage();
  await page.goto(WAKE);
  await page.waitForSelector("#settings[open]");
  await page.fill("#f-mac", "zz:11");
  await page.fill("#f-host", "");
  await page.fill("#f-port", "70000");
  await page.fill("#f-url", "ftp://x");
  await page.click("#f-save");
  const err = await page.textContent("#f-err");
  for (const word of ["MAC", "Adresse", "Port", "JARVIS-Adresse"]) assert.ok(err.includes(word), err);
  assert.equal(await page.evaluate(() => localStorage.getItem("jarvis.wake.settings")), null);

  await page.fill("#f-mac", "aa-bb-cc-dd-ee-ff");
  await page.fill("#f-host", "https://beispiel.myfritz.net/");
  await page.fill("#f-port", "9");
  await page.fill("#f-url", JARVIS + "/");
  await page.click("#f-save");
  await page.waitForFunction(() => !document.getElementById("settings").open);
  const saved = JSON.parse(await page.evaluate(() => localStorage.getItem("jarvis.wake.settings")));
  assert.deepEqual(saved, SETTINGS);
  await context.close();
});

await test("PC starten: Depicus-URL, Polling alle 3 s, dann Weiterleitung zu JARVIS", async () => {
  const { context, page } = await newPage({ settings: SETTINGS });
  const calls = [];
  await context.route(JARVIS + "/api/health*", async (route) => {
    calls.push({ t: Date.now() });
    if (calls.length < 3) return route.abort("connectionrefused");
    return route.fulfill({ status: 200, body: '{"status":"ok"}' });
  });
  await context.route(JARVIS + "/", (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<title>FAKE JARVIS</title>" }));
  await page.goto(WAKE);
  await page.click("#wake");
  const opened = await page.evaluate(() => window.__opened);
  assert.deepEqual(opened, ["https://www.depicus.com/wake-on-lan/woli?m=AABBCCDDEEFF&i=beispiel.myfritz.net&s=255.255.255.255&p=9"]);
  assert.match(await page.textContent("#status"), /Warte auf PC/);
  await page.waitForSelector("#open-jarvis:not([hidden])", { timeout: 10000 });
  assert.equal(calls.length, 3);
  const gaps = calls.slice(1).map((c, i) => c.t - calls[i].t);
  for (const g of gaps) assert.ok(g > 2700 && g < 3600, "Abstand " + g + " ms");
  const opts = await page.evaluate(() => window.__fetchOpts);
  assert.ok(opts.length === 3 && opts.every((o) => o.mode === "no-cors" && o.signal), JSON.stringify(opts));
  assert.match(await page.textContent("#status"), /online/);
  await page.waitForURL(JARVIS + "/", { timeout: 6000 });
  assert.equal(await page.title(), "FAKE JARVIS");
  await context.close();
});

await test("hängende Abfrage wird nach 2,5 s abgebrochen und als offline gewertet", async () => {
  const { context, page } = await newPage({ settings: SETTINGS });
  const calls = [];
  await context.route(JARVIS + "/api/health*", async (route) => {
    calls.push(Date.now());
    if (calls.length === 1) { await new Promise((r) => setTimeout(r, 4000)); return route.abort().catch(() => {}); }
    return route.fulfill({ status: 200, body: "ok" });
  });
  await page.goto(WAKE);
  await page.click("#wake");
  await page.waitForSelector("#open-jarvis:not([hidden])", { timeout: 8000 });
  assert.equal(calls.length, 2);
  const gap = calls[1] - calls[0];
  assert.ok(gap > 2700 && gap < 3600, "zweite Abfrage nach " + gap + " ms (erwartet ~3000)");
  await page.click("#stay");
  await page.waitForTimeout(3500);
  assert.equal(page.url(), WAKE, "nach 'Hier bleiben' keine Weiterleitung");
  await context.close();
});

await test("blockiertes Pop-up zeigt einen Link zur Wake-Seite", async () => {
  const { context, page } = await newPage({ settings: SETTINGS, popupBlocked: true });
  await context.route(JARVIS + "/api/health*", (route) => route.abort("connectionrefused"));
  await page.goto(WAKE);
  await page.click("#wake");
  assert.ok(await page.isVisible("#wake-link"));
  assert.match(await page.getAttribute("#wake-link", "href"), /^https:\/\/www\.depicus\.com\/wake-on-lan\/woli\?m=AABBCCDDEEFF&/);
  await page.click("#stop-wait");
  assert.match(await page.textContent("#status"), /abgebrochen/);
  await context.close();
});

await test("JARVIS direkt öffnen navigiert sofort", async () => {
  const { context, page } = await newPage({ settings: SETTINGS });
  await context.route(JARVIS + "/", (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<title>FAKE JARVIS</title>" }));
  await page.goto(WAKE);
  await page.click("#direct");
  await page.waitForURL(JARVIS + "/");
  await context.close();
});

await browser.close();
if (failures) { console.log(failures + " Test(s) fehlgeschlagen"); process.exit(1); }
console.log("alle wake.html-Tests bestanden");
