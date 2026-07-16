import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { chromium } from "playwright";

const STOCK = Object.freeze({
  IN_STOCK: "In Stock",
  OOS: "OOS",
  LOW_STOCK: "Low Stock",
  LIMITED_STOCK: "Limited Stock",
  QUANTITY: "Only Quantity Remaining",
  UNKNOWN: "Unable to Verify",
});

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => { data += chunk; });
    process.stdin.on("end", () => {
      try {
        resolve(JSON.parse(data || "{}"));
      } catch (error) {
        reject(new Error(`Invalid request JSON: ${error.message}`));
      }
    });
    process.stdin.on("error", reject);
  });
}

function loadConfig(configPath) {
  const defaults = {
    headless: true,
    browser_channel: "chromium",
    navigation_timeout_ms: 45000,
    settle_timeout_ms: 12000,
    fixed_zip_code: "",
    profile_directory: "browser_profiles/walmart_playwright",
    save_debug_evidence: true,
    screenshots_on_error: true,
    block_images: false,
  };

  try {
    const parsed = JSON.parse(fs.readFileSync(configPath, "utf8"));
    return { ...defaults, ...parsed };
  } catch {
    return defaults;
  }
}

function exactItemId(url) {
  const parsed = new URL(url);
  const segments = parsed.pathname.split("/").filter(Boolean);
  const ipIndex = segments.findIndex(value => value.toLowerCase() === "ip");

  if (ipIndex >= 0) {
    for (let i = segments.length - 1; i > ipIndex; i -= 1) {
      if (/^\d{5,}$/.test(segments[i])) return segments[i];
    }
  }

  const matches = parsed.pathname.match(/\/(\d{5,})(?:\/|$)/g) || [];
  if (matches.length) return matches.at(-1).replace(/\//g, "");

  for (const key of ["itemId", "item_id", "usItemId"]) {
    const value = parsed.searchParams.get(key);
    if (value && /^\d{5,}$/.test(value)) return value;
  }

  return "";
}

function normalizeText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function normalizePrice(value) {
  if (value === null || value === undefined) return "";

  if (typeof value === "number" && Number.isFinite(value)) {
    return value.toFixed(2);
  }

  const text = normalizeText(value).replace(/,/g, "");
  const match = text.match(/\$?\s*(\d{1,7}(?:\.\d{1,2})?)/);
  if (!match) return "";

  const amount = Number(match[1]);
  return Number.isFinite(amount) ? amount.toFixed(2) : "";
}

function stockFromText(value) {
  const text = normalizeText(value).toLowerCase();
  if (!text) return STOCK.UNKNOWN;

  if (/back[\s-]?order|sold out|out of stock|currently unavailable|item unavailable/.test(text)) {
    return STOCK.OOS;
  }

  if (/limited stock/.test(text)) return STOCK.LIMITED_STOCK;
  if (/low stock/.test(text)) return STOCK.LOW_STOCK;

  if (/only\s+\d+\s+(?:left|remaining|in stock)/.test(text)) {
    return STOCK.QUANTITY;
  }

  if (/\bin stock\b|available for shipping|shipping available/.test(text)) {
    return STOCK.IN_STOCK;
  }

  return STOCK.UNKNOWN;
}

function businessStock(stock) {
  if (
    stock === STOCK.LOW_STOCK ||
    stock === STOCK.LIMITED_STOCK ||
    stock === STOCK.QUANTITY
  ) {
    return stock;
  }
  return stock;
}

function objectItemIds(object) {
  const values = [];
  const keys = [
    "usItemId",
    "itemId",
    "item_id",
    "productId",
    "product_id",
    "offerId",
  ];

  for (const key of keys) {
    const value = object?.[key];
    if (value !== null && value !== undefined) {
      values.push(String(value));
    }
  }

  return values;
}

function exactObjects(root, itemId) {
  const matches = [];
  const seen = new WeakSet();
  const stack = [{ value: root, path: "$", depth: 0 }];

  while (stack.length) {
    const current = stack.pop();
    const value = current.value;

    if (!value || typeof value !== "object") continue;
    if (seen.has(value)) continue;
    seen.add(value);

    if (!Array.isArray(value)) {
      const ids = objectItemIds(value);
      if (ids.includes(itemId)) {
        matches.push({
          object: value,
          path: current.path,
          depth: current.depth,
        });
      }
    }

    if (current.depth >= 16) continue;

    if (Array.isArray(value)) {
      for (let i = 0; i < Math.min(value.length, 500); i += 1) {
        stack.push({
          value: value[i],
          path: `${current.path}[${i}]`,
          depth: current.depth + 1,
        });
      }
    } else {
      const entries = Object.entries(value);
      for (let i = 0; i < Math.min(entries.length, 500); i += 1) {
        const [key, child] = entries[i];
        stack.push({
          value: child,
          path: `${current.path}.${key}`,
          depth: current.depth + 1,
        });
      }
    }
  }

  return matches;
}

function candidateStrings(object, maxDepth = 5) {
  const found = [];
  const seen = new WeakSet();
  const stack = [{ value: object, depth: 0, key: "" }];

  while (stack.length) {
    const { value, depth, key } = stack.pop();

    if (value === null || value === undefined) continue;

    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      found.push({ key, value });
      continue;
    }

    if (typeof value !== "object" || seen.has(value) || depth >= maxDepth) continue;
    seen.add(value);

    if (Array.isArray(value)) {
      for (const child of value.slice(0, 100)) {
        stack.push({ value: child, depth: depth + 1, key });
      }
    } else {
      for (const [childKey, child] of Object.entries(value).slice(0, 200)) {
        stack.push({
          value: child,
          depth: depth + 1,
          key: childKey,
        });
      }
    }
  }

  return found;
}

function extractStructuredCandidate(match) {
  const values = candidateStrings(match.object);
  const titleKeys = /^(name|title|productName|productTitle)$/i;
  const priceKeys = /(currentPrice|price|priceValue|linePrice|itemPrice|displayPrice)/i;
  const stockKeys = /(availabilityStatus|availability|stockStatus|inventoryStatus|offerAvailability|fulfillmentStatus)/i;
  const sellerKeys = /(sellerName|sellerDisplayName|seller|soldBy)/i;
  const selectedKeys = /(selected|isSelected|currentSelection)/i;

  let title = "";
  let price = "";
  let stock = STOCK.UNKNOWN;
  let seller = "";
  let selectedScore = 0;

  for (const entry of values) {
    const key = String(entry.key || "");
    const value = entry.value;

    if (!title && titleKeys.test(key) && typeof value === "string") {
      const text = normalizeText(value);
      if (text.length >= 3 && text.length <= 300) title = text;
    }

    if (!price && priceKeys.test(key)) {
      const parsed = normalizePrice(value);
      if (parsed) price = parsed;
    }

    if (stock === STOCK.UNKNOWN && stockKeys.test(key)) {
      const parsed = stockFromText(value);
      if (parsed !== STOCK.UNKNOWN) stock = parsed;
    }

    if (!seller && sellerKeys.test(key) && typeof value === "string") {
      seller = normalizeText(value);
    }

    if (selectedKeys.test(key)) {
      if (value === true || String(value).toLowerCase() === "true") selectedScore += 5;
    }
  }

  const serialized = normalizeText(JSON.stringify(match.object)).slice(0, 12000);
  if (stock === STOCK.UNKNOWN) stock = stockFromText(serialized);

  let score = 100;
  if (title) score += 10;
  if (price) score += 20;
  if (stock !== STOCK.UNKNOWN) score += 30;
  if (seller) score += 5;
  score += selectedScore;
  score -= Math.min(match.depth, 20);

  if (/recommend|sponsor|carousel|similar|related/i.test(match.path)) score -= 50;
  if (/buyBox|purchase|offer|product|item/i.test(match.path)) score += 10;

  return {
    score,
    title,
    price,
    stock,
    seller,
    path: match.path,
  };
}

function bestStructuredEvidence(jsonBodies, itemId) {
  const candidates = [];

  for (const body of jsonBodies) {
    for (const match of exactObjects(body.json, itemId)) {
      const candidate = extractStructuredCandidate(match);
      candidate.responseUrl = body.url;
      candidates.push(candidate);
    }
  }

  candidates.sort((a, b) => b.score - a.score);
  return {
    best: candidates[0] || null,
    candidates: candidates.slice(0, 8),
  };
}

async function readNextData(page) {
  return page.evaluate(() => {
    const scripts = [
      document.querySelector("script#__NEXT_DATA__"),
      ...document.querySelectorAll('script[type="application/json"]'),
    ].filter(Boolean);

    const results = [];
    for (const script of scripts.slice(0, 30)) {
      const text = script.textContent || "";
      if (!text.trim()) continue;
      try {
        results.push(JSON.parse(text));
      } catch {
        // Ignore non-JSON scripts.
      }
    }
    return results;
  });
}

async function exactDomEvidence(page, itemId) {
  return page.evaluate(({ itemId }) => {
    const normalize = value => String(value || "").replace(/\s+/g, " ").trim();
    const visible = element => {
      if (!element) return false;
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return (
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        Number(style.opacity || 1) > 0 &&
        rect.width > 0 &&
        rect.height > 0
      );
    };

    const itemTokens = [
      `[data-item-id="${CSS.escape(itemId)}"]`,
      `[data-us-item-id="${CSS.escape(itemId)}"]`,
      `[data-product-id="${CSS.escape(itemId)}"]`,
      `[data-testid*="${CSS.escape(itemId)}"]`,
    ];

    let exactRoot = null;
    for (const selector of itemTokens) {
      const candidate = document.querySelector(selector);
      if (candidate && visible(candidate)) {
        exactRoot = candidate;
        break;
      }
    }

    const main =
      exactRoot?.closest("main") ||
      document.querySelector("main") ||
      document.querySelector('[role="main"]') ||
      document.body;

    const headings = [...main.querySelectorAll("h1")].filter(visible);
    const title = normalize(headings[0]?.textContent || document.title);

    const priceMeta =
      document.querySelector('meta[itemprop="price"]')?.content ||
      document.querySelector('meta[property="product:price:amount"]')?.content ||
      "";

    const priceSelectors = [
      '[data-testid="price-wrap"]',
      '[itemprop="price"]',
      '[data-automation-id*="price"]',
      '[data-testid*="price"]',
    ];

    let priceText = normalize(priceMeta);
    for (const selector of priceSelectors) {
      if (priceText) break;
      const candidates = [...main.querySelectorAll(selector)].filter(visible);
      for (const candidate of candidates) {
        const text = normalize(candidate.textContent);
        if (/\$\s*\d/.test(text)) {
          priceText = text;
          break;
        }
      }
    }

    const buttons = [...main.querySelectorAll("button")].filter(visible);
    const addButtons = buttons.filter(button =>
      /add to cart|add to basket/i.test(normalize(button.textContent))
    );

    const enabledAdd = addButtons.find(button =>
      !button.disabled &&
      button.getAttribute("aria-disabled") !== "true"
    );

    const disabledAdd = addButtons.find(button =>
      button.disabled ||
      button.getAttribute("aria-disabled") === "true"
    );

    const rootForOffer =
      enabledAdd?.closest(
        '[data-testid*="buy"], [data-testid*="purchase"], section, article, form'
      ) ||
      disabledAdd?.closest(
        '[data-testid*="buy"], [data-testid*="purchase"], section, article, form'
      ) ||
      main;

    const offerText = normalize(rootForOffer.textContent).slice(0, 12000);

    const atomicTexts = [...rootForOffer.querySelectorAll("span, p, div, button")]
      .filter(visible)
      .map(element => normalize(
        [...element.childNodes]
          .filter(node => node.nodeType === Node.TEXT_NODE)
          .map(node => node.textContent)
          .join(" ")
      ))
      .filter(text => text && text.length <= 180);

    const exactStockTexts = atomicTexts.filter(text =>
      /low stock|limited stock|only\s+\d+\s+(?:left|remaining|in stock)|out of stock|sold out|back[\s-]?order/i
        .test(text)
    );

    const fulfillment = {};
    for (const label of ["shipping", "pickup", "delivery"]) {
      const labelNodes = [...rootForOffer.querySelectorAll("span, p, div, h3, h4")]
        .filter(visible)
        .filter(element => normalize(element.textContent).toLowerCase() === label);

      let best = null;
      for (const labelNode of labelNodes) {
        let current = labelNode.parentElement;
        for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
          if (!visible(current)) continue;
          const text = normalize(current.textContent);
          if (!text || text.length > 500) continue;

          const otherLabels = ["shipping", "pickup", "delivery"]
            .filter(other => other !== label)
            .filter(other => new RegExp(`\\b${other}\\b`, "i").test(text))
            .length;

          const candidate = { text, otherLabels, depth };
          if (
            !best ||
            candidate.otherLabels < best.otherLabels ||
            (
              candidate.otherLabels === best.otherLabels &&
              candidate.depth < best.depth
            ) ||
            (
              candidate.otherLabels === best.otherLabels &&
              candidate.depth === best.depth &&
              candidate.text.length < best.text.length
            )
          ) {
            best = candidate;
          }
        }
      }

      if (best) fulfillment[label] = best.text;
    }

    return {
      title,
      priceText,
      enabledAdd: Boolean(enabledAdd),
      disabledAdd: Boolean(disabledAdd),
      exactStockTexts,
      offerText,
      fulfillment,
      pageUrl: location.href,
    };
  }, { itemId });
}

function domStock(dom) {
  const exact = dom.exactStockTexts || [];

  for (const text of exact) {
    const state = stockFromText(text);
    if (
      state === STOCK.LOW_STOCK ||
      state === STOCK.LIMITED_STOCK ||
      state === STOCK.QUANTITY ||
      state === STOCK.OOS
    ) {
      return { stock: state, evidence: text };
    }
  }

  if (dom.enabledAdd) {
    return { stock: STOCK.IN_STOCK, evidence: "Enabled primary Add to cart" };
  }

  if (dom.disabledAdd) {
    return { stock: STOCK.OOS, evidence: "Disabled primary Add to cart" };
  }

  const fulfillmentStates = Object.entries(dom.fulfillment || {}).map(
    ([method, text]) => ({ method, text, stock: stockFromText(text) })
  );

  if (fulfillmentStates.some(entry => entry.stock === STOCK.IN_STOCK)) {
    return {
      stock: STOCK.IN_STOCK,
      evidence: fulfillmentStates
        .filter(entry => entry.stock === STOCK.IN_STOCK)
        .map(entry => `${entry.method}: ${entry.text}`)
        .join(" | "),
    };
  }

  if (
    fulfillmentStates.length >= 2 &&
    fulfillmentStates.every(entry => entry.stock === STOCK.OOS)
  ) {
    return {
      stock: STOCK.OOS,
      evidence: fulfillmentStates
        .map(entry => `${entry.method}: ${entry.text}`)
        .join(" | "),
    };
  }

  return { stock: STOCK.UNKNOWN, evidence: "" };
}

function finalDecision(structured, dom) {
  const structuredStock = structured?.stock || STOCK.UNKNOWN;
  const domResult = domStock(dom);
  const domStockValue = domResult.stock;

  // Exact low-inventory wording in the selected primary offer is more
  // specific than generic structured "IN_STOCK".
  if (
    domStockValue === STOCK.LOW_STOCK ||
    domStockValue === STOCK.LIMITED_STOCK ||
    domStockValue === STOCK.QUANTITY
  ) {
    return {
      stock: domStockValue,
      reason: `Selected offer reports ${domResult.evidence}.`,
      conflict: false,
    };
  }

  // Explicit selected-offer OOS overrides generic structured availability.
  if (domStockValue === STOCK.OOS && !dom.enabledAdd) {
    return {
      stock: STOCK.OOS,
      reason: `Selected offer reports OOS: ${domResult.evidence}.`,
      conflict: false,
    };
  }

  // A valid primary CTA for the exact linked page is strong availability
  // evidence; unrelated variant OOS text is excluded by the scoped DOM pass.
  if (domStockValue === STOCK.IN_STOCK && dom.enabledAdd) {
    return {
      stock: STOCK.IN_STOCK,
      reason: "The exact linked item's primary offer has an enabled Add to cart control.",
      conflict: false,
    };
  }

  if (structuredStock !== STOCK.UNKNOWN && domStockValue === STOCK.UNKNOWN) {
    return {
      stock: structuredStock,
      reason: "Exact-item structured data supplied the stock result.",
      conflict: false,
    };
  }

  if (structuredStock === STOCK.UNKNOWN && domStockValue !== STOCK.UNKNOWN) {
    return {
      stock: domStockValue,
      reason: `Exact-item primary offer supplied the stock result: ${domResult.evidence}.`,
      conflict: false,
    };
  }

  if (structuredStock === domStockValue && structuredStock !== STOCK.UNKNOWN) {
    return {
      stock: structuredStock,
      reason: "Exact-item structured data and the selected primary offer agree.",
      conflict: false,
    };
  }

  return {
    stock: STOCK.UNKNOWN,
    reason: (
      "The exact-item structured data and selected primary offer do not " +
      `produce one reliable stock state (${structuredStock} versus ${domStockValue}).`
    ),
    conflict: true,
  };
}

async function maybeSetZip(page, zipCode) {
  if (!zipCode || !/^\d{5}$/.test(zipCode)) return false;

  // Walmart changes this UI frequently. This conservative helper only acts
  // when a clearly labelled location control and ZIP input are present.
  const locationButtons = [
    page.getByRole("button", { name: /shipping to|location|how do you want your items/i }),
    page.getByText(/shipping to|location/i, { exact: false }),
  ];

  for (const locator of locationButtons) {
    try {
      const first = locator.first();
      if (await first.isVisible({ timeout: 1200 })) {
        await first.click({ timeout: 3000 });
        break;
      }
    } catch {
      // Try the next explicit location control.
    }
  }

  const inputs = [
    page.getByLabel(/zip code/i),
    page.getByPlaceholder(/zip code/i),
    page.locator('input[name*="postal"], input[id*="postal"], input[inputmode="numeric"]'),
  ];

  for (const locator of inputs) {
    try {
      const first = locator.first();
      if (await first.isVisible({ timeout: 1200 })) {
        await first.fill(zipCode);
        const buttons = [
          page.getByRole("button", { name: /apply|save|update|submit/i }),
          page.locator('button[type="submit"]'),
        ];
        for (const button of buttons) {
          try {
            const firstButton = button.first();
            if (await firstButton.isVisible({ timeout: 800 })) {
              await firstButton.click({ timeout: 3000 });
              await page.waitForTimeout(1800);
              return true;
            }
          } catch {
            // Try another explicit submit button.
          }
        }
      }
    } catch {
      // Location setup is optional; never guess or click unrelated controls.
    }
  }

  return false;
}

function safeFileName(value) {
  return String(value).replace(/[^a-z0-9._-]+/gi, "_").slice(0, 120);
}

async function main() {
  const request = await readStdin();
  const url = normalizeText(request.url);
  const projectRoot = path.resolve(request.projectRoot || process.cwd());
  const configPath = path.resolve(
    request.configPath || path.join(projectRoot, "config", "walmart_playwright.json")
  );
  const config = loadConfig(configPath);
  const itemId = exactItemId(url);

  if (!url || !/^https?:\/\/(?:www\.)?walmart\.com\//i.test(url)) {
    throw new Error("The supplied link is not a Walmart product URL.");
  }
  if (!itemId) {
    throw new Error("Could not identify the exact Walmart item ID from the link.");
  }

  const profileDir = path.resolve(projectRoot, config.profile_directory);
  fs.mkdirSync(profileDir, { recursive: true });

  const debugDir = path.join(projectRoot, "logs", "walmart_evidence");
  if (config.save_debug_evidence) fs.mkdirSync(debugDir, { recursive: true });

  const context = await chromium.launchPersistentContext(profileDir, {
    headless: process.env.WALMART_PROFILE_VISIBLE === "1" ? false : Boolean(config.headless),
    viewport: { width: 1440, height: 1100 },
    locale: "en-US",
    timezoneId: "America/Chicago",
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
      "AppleWebKit/537.36 (KHTML, like Gecko) " +
      "Chrome/138.0.0.0 Safari/537.36",
    args: [
      "--disable-blink-features=AutomationControlled",
      "--disable-notifications",
    ],
  });

  let page = context.pages()[0] || await context.newPage();
  page.setDefaultNavigationTimeout(config.navigation_timeout_ms);
  page.setDefaultTimeout(12000);

  const jsonBodies = [];
  page.on("response", async response => {
    try {
      const contentType = response.headers()["content-type"] || "";
      if (!/json/i.test(contentType)) return;
      const responseUrl = response.url();
      if (!/walmart|graphql|product|item|offer|fulfill|inventory/i.test(responseUrl)) return;
      const json = await response.json();
      jsonBodies.push({ url: responseUrl, json });
    } catch {
      // Ignore responses that are not readable JSON.
    }
  });

  if (config.block_images) {
    await page.route("**/*", route => {
      const type = route.request().resourceType();
      if (["image", "font", "media"].includes(type)) route.abort();
      else route.continue();
    });
  }

  let result;

  try {
    await page.goto(url, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle", { timeout: config.settle_timeout_ms })
      .catch(() => {});
    await page.waitForTimeout(1800);

    const blockedText = normalizeText(await page.locator("body").innerText().catch(() => ""));
    if (/verify you are human|press and hold|robot or human|access denied|captcha/i.test(blockedText)) {
      throw new Error(
        "Walmart requested human verification. Open the dedicated Walmart " +
        "Playwright profile once in visible mode, complete the check, then retry."
      );
    }

    if (config.fixed_zip_code) {
      const changed = await maybeSetZip(page, config.fixed_zip_code);
      if (changed) {
        await page.reload({ waitUntil: "domcontentloaded" });
        await page.waitForLoadState("networkidle", { timeout: config.settle_timeout_ms })
          .catch(() => {});
        await page.waitForTimeout(1200);
      }
    }

    const embeddedJson = await readNextData(page);
    for (const json of embeddedJson) {
      jsonBodies.push({ url: "embedded://page-json", json });
    }

    const structuredResult = bestStructuredEvidence(jsonBodies, itemId);
    const dom = await exactDomEvidence(page, itemId);
    const decision = finalDecision(structuredResult.best, dom);

    const structuredPrice = structuredResult.best?.price || "";
    const domPrice = normalizePrice(dom.priceText);
    const price = structuredPrice || domPrice;

    const title =
      structuredResult.best?.title ||
      dom.title ||
      `Walmart item ${itemId}`;

    const finalPageItemId = exactItemId(dom.pageUrl);
    if (finalPageItemId && finalPageItemId !== itemId) {
      throw new Error(
        `Walmart redirected item ${itemId} to a different item ${finalPageItemId}.`
      );
    }

    if (decision.stock === STOCK.UNKNOWN) {
      result = {
        status: "Error",
        itemId,
        title,
        price: "",
        stock: STOCK.UNKNOWN,
        reason: decision.reason,
        evidence: {
          requestedItemId: itemId,
          pageUrl: dom.pageUrl,
          structured: structuredResult,
          dom,
        },
      };
    } else {
      result = {
        status: "Success",
        itemId,
        title,
        price,
        stock: businessStock(decision.stock),
        reason: decision.reason,
        evidence: {
          requestedItemId: itemId,
          pageUrl: dom.pageUrl,
          structuredBest: structuredResult.best,
          structuredCandidates: structuredResult.candidates,
          dom: {
            title: dom.title,
            priceText: dom.priceText,
            enabledAdd: dom.enabledAdd,
            disabledAdd: dom.disabledAdd,
            exactStockTexts: dom.exactStockTexts,
            fulfillment: dom.fulfillment,
          },
        },
      };
    }

    if (config.save_debug_evidence) {
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      const base = `${stamp}_${safeFileName(itemId)}`;
      fs.writeFileSync(
        path.join(debugDir, `${base}.json`),
        JSON.stringify(result, null, 2),
        "utf8"
      );

      if (result.status !== "Success" && config.screenshots_on_error) {
        await page.screenshot({
          path: path.join(debugDir, `${base}.png`),
          fullPage: true,
        }).catch(() => {});
      }
    }
  } finally {
    await context.close();
  }

  process.stdout.write(JSON.stringify(result));
}

main().catch(error => {
  process.stderr.write(String(error?.stack || error?.message || error));
  process.exitCode = 1;
});
