import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const data = JSON.parse(
  fs.readFileSync("recalls.json", "utf8")
);

const recalls = Array.isArray(data.recalls)
  ? data.recalls
  : [];

const outDir = ".quality/pdf";
fs.mkdirSync(outDir, { recursive: true });

function clean(value) {
  return String(value || "").trim();
}

async function challengeOk(page) {
  for (let attempt = 0; attempt < 5; attempt++) {
    await page.waitForTimeout(2500);

    const body = await page
      .locator("body")
      .innerText()
      .catch(() => "");

    const blocked =
      /browser validation|please enable javascript|please enable cookies/i
        .test(body);

    if (!blocked) {
      return true;
    }

    await page.reload({
      waitUntil: "domcontentloaded",
      timeout: 90000,
    });
  }

  return false;
}

async function refreshSession(page) {
  await page.goto(
    "https://www.salute.gov.it/",
    {
      waitUntil: "domcontentloaded",
      timeout: 90000,
    }
  );

  if (!(await challengeOk(page))) {
    throw new Error("Protezione Ministero non superata");
  }
}

async function downloadPdf(context, page, pdfUrl) {
  let lastError = null;

  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const response = await context.request.get(
        pdfUrl,
        {
          timeout: 90000,
          failOnStatusCode: false,
        }
      );

      const buffer = Buffer.from(
        await response.body()
      );

      if (
        response.ok() &&
        response.status() === 200 &&
        buffer.length >= 1000 &&
        buffer.subarray(0, 5).toString() === "%PDF-"
      ) {
        return buffer;
      }

      lastError = new Error(
        `PDF non valido HTTP ${response.status()}`
      );

    } catch (error) {
      lastError = error;
    }

    if (attempt < 3) {
      await refreshSession(page);
      await page.waitForTimeout(1200 * attempt);
    }
  }

  throw lastError || new Error("Download PDF fallito");
}

const browser = await chromium.launch({
  headless: true,
});

const context = await browser.newContext({
  locale: "it-IT",
  userAgent:
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
    "AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/131.0.0.0 Safari/537.36",
});

try {
  const page = await context.newPage();
  await refreshSession(page);

  let downloaded = 0;
  let skipped = 0;
  let failed = 0;

  for (const recall of recalls) {
    const id = clean(recall.id);
    const pdfUrl = clean(recall.pdfMinistero);

    if (!id || !pdfUrl) {
      skipped++;
      continue;
    }

    const target = path.join(
      outDir,
      `${id}.pdf`
    );

    try {
      const buffer = await downloadPdf(
        context,
        page,
        pdfUrl
      );

      fs.writeFileSync(target, buffer);
      downloaded++;

      console.log("✅ PDF immagini:", id);

    } catch (error) {
      failed++;

      console.log(
        "⚠️ PDF non scaricato:",
        id,
        String(error?.message || error)
      );
    }
  }

  console.log("");
  console.log("PDF scaricati:", downloaded);
  console.log("PDF senza URL/saltati:", skipped);
  console.log("PDF falliti:", failed);

} finally {
  await context.close().catch(() => {});
  await browser.close();
}
