import { chromium } from "playwright";
import fs from "fs";

const FEED_URL = "https://www.salute.gov.it/new/rss/RSS_avvisi_richiami_osa.xml";
const STATE_PATH = "notification-state.json";
const PENDING_PATH = "notification-pending.json";

function clean(value) {
  return String(value || "")
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+/g, " ")
    .trim();
}

function decodeXml(value) {
  return String(value || "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function extractTag(block, tag) {
  const regex = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)<\\/${tag}>`, "i");
  const match = block.match(regex);
  if (!match) return "";
  let value = match[1]
    .replace(/^<!\[CDATA\[/, "")
    .replace(/\]\]>$/, "");
  return clean(decodeXml(value).replace(/<[^>]+>/g, " "));
}

function safeId(urlString) {
  try {
    const url = new URL(urlString);
    const parts = url.pathname.split("/").filter(Boolean);
    return (parts[parts.length - 1] || "")
      .toLowerCase()
      .replace(/[^a-z0-9_-]/g, "-")
      .replace(/-+/g, "-")
      .replace(/^-|-$/g, "");
  } catch {
    return "";
  }
}

function parseRss(xml) {
  const blocks = xml.match(/<item\b[\s\S]*?<\/item>/gi) || [];
  const seen = new Set();
  const items = [];

  for (const block of blocks) {
    const link = extractTag(block, "link");
    if (!link || !link.includes("/ext-avviso-sicurezza-alimentare/")) continue;

    const id = safeId(link);
    if (!id || seen.has(id)) continue;
    seen.add(id);

    items.push({
      id,
      link,
      title: extractTag(block, "title"),
      pubDate: extractTag(block, "pubDate"),
      description: extractTag(block, "description")
    });
  }

  return items;
}

async function superaChallenge(page) {
  for (let attempt = 0; attempt < 5; attempt++) {
    await page.waitForTimeout(3000);
    const body = await page.locator("body").innerText().catch(() => "");
    const blocked =
      /browser validation|please enable javascript|please enable cookies/i.test(body);

    if (!blocked) return true;

    await page.reload({
      waitUntil: "domcontentloaded",
      timeout: 90000
    });
  }

  return false;
}

function pageField(body, labels) {
  for (const label of labels) {
    const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const regex = new RegExp(
      escaped + "\\s*:?\\s*([^\\n\\r]+)",
      "i"
    );
    const match = body.match(regex);
    if (match && match[1]) {
      const value = clean(match[1]);
      if (value && value.length < 500) return value;
    }
  }
  return "";
}

async function enrichNewItem(context, item) {
  const page = await context.newPage();

  try {
    await page.goto(item.link, {
      waitUntil: "domcontentloaded",
      timeout: 90000
    });

    if (!(await superaChallenge(page))) {
      throw new Error("Protezione Ministero non superata");
    }

    await page.waitForTimeout(1200);
    const body = await page.locator("body").innerText();

    const enriched = {
      ...item,
      marca: pageField(body, ["Marca", "Marchio"]),
      prodotto: pageField(body, ["Denominazione"]) || item.title,
      motivo: pageField(body, ["Motivo della segnalazione"]),
      dataPubblicazione:
        pageField(body, ["Data pubblicazione"]) || item.pubDate
    };

    console.log(
      "DETTAGLI:",
      item.id,
      "- motivo:",
      enriched.motivo || "(non disponibile)"
    );

    return enriched;
  } catch (error) {
    console.log(
      "⚠️ Dettagli immediati non disponibili:",
      item.id,
      String(error?.message || error)
    );
    return item;
  } finally {
    await page.close().catch(() => {});
  }
}

function loadState() {
  if (!fs.existsSync(STATE_PATH)) {
    return { version: 1, notifiedIds: [] };
  }

  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, "utf8"));
  } catch {
    return { version: 1, notifiedIds: [] };
  }
}

const browser = await chromium.launch({ headless: true });

try {
  const context = await browser.newContext({
    locale: "it-IT",
    viewport: { width: 1400, height: 1000 },
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
      "AppleWebKit/537.36 (KHTML, like Gecko) " +
      "Chrome/131.0.0.0 Safari/537.36"
  });

  const page = await context.newPage();

  await page.goto("https://www.salute.gov.it/", {
    waitUntil: "domcontentloaded",
    timeout: 90000
  });

  if (!(await superaChallenge(page))) {
    throw new Error("Protezione Ministero non superata");
  }

  const result = await page.evaluate(async url => {
    const response = await fetch(url, { credentials: "include" });
    return {
      ok: response.ok,
      status: response.status,
      text: await response.text()
    };
  }, FEED_URL);

  if (!result.ok || result.status !== 200 || !result.text.includes("<item")) {
    throw new Error(`Feed RSS non valido: HTTP ${result.status}`);
  }

  const items = parseRss(result.text);
  if (!items.length) {
    throw new Error("Feed RSS senza richiami");
  }

  const state = loadState();
  const notified = new Set(
    (state.notifiedIds || [])
      .map(value => String(value || "").trim())
      .filter(Boolean)
  );

  const newItems = items.filter(item => !notified.has(item.id));

  const enrichedNewItems = [];
  for (const item of newItems) {
    enrichedNewItems.push(
      await enrichNewItem(context, item)
    );
  }

  const pending = {
    checkedAt: new Date().toISOString(),
    feedUrl: FEED_URL,
    totalFeedItems: items.length,
    feedIds: items.map(item => item.id),
    newItems: enrichedNewItems
  };

  fs.writeFileSync(PENDING_PATH, JSON.stringify(pending, null, 2) + "\n", "utf8");

  console.log("Richiami nel feed:", items.length);
  console.log("ID già notificati:", notified.size);
  console.log("Nuovi richiami da notificare:", enrichedNewItems.length);

  for (const item of enrichedNewItems) {
    console.log("NUOVO:", item.id, "-", item.title || item.link);
  }

  await page.close();
  await context.close();
} finally {
  await browser.close();
}
