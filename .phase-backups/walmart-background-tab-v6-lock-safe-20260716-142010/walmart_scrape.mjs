import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const VERSION = "2026.07.16.pure-brave-cdp-v4";

const STOCK = Object.freeze({
  IN_STOCK: "In Stock",
  OOS: "OOS",
  LOW_STOCK: "Low Stock",
  LIMITED_STOCK: "Limited Stock",
  QUANTITY: "Only Quantity Remaining",
  UNKNOWN: "Unable to Verify",
});

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function normalizeText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function exactItemId(url) {
  const parsed = new URL(url);
  const segments = parsed.pathname.split("/").filter(Boolean);
  const ipIndex = segments.findIndex(
    value => value.toLowerCase() === "ip"
  );

  if (ipIndex >= 0) {
    for (let index = segments.length - 1; index > ipIndex; index -= 1) {
      if (/^\d{5,}$/.test(segments[index])) {
        return segments[index];
      }
    }
  }

  for (const key of ["itemId", "item_id", "usItemId"]) {
    const value = parsed.searchParams.get(key);
    if (value && /^\d{5,}$/.test(value)) {
      return value;
    }
  }

  return "";
}

function normalizePrice(value) {
  if (value === null || value === undefined) {
    return "";
  }

  if (typeof value === "number" && Number.isFinite(value)) {
    return value.toFixed(2);
  }

  const text = normalizeText(value).replace(/,/g, "");
  const match = text.match(/\$?\s*(\d{1,7}(?:\.\d{1,2})?)/);

  if (!match) {
    return "";
  }

  const number = Number(match[1]);
  return Number.isFinite(number) ? number.toFixed(2) : "";
}

function stockFromText(value) {
  const text = normalizeText(value).toLowerCase();

  if (!text) {
    return STOCK.UNKNOWN;
  }

  if (/back[\s-]?order|sold out|out of stock|currently unavailable|item unavailable/.test(text)) {
    return STOCK.OOS;
  }

  if (/limited stock/.test(text)) {
    return STOCK.LIMITED_STOCK;
  }

  if (/low stock/.test(text)) {
    return STOCK.LOW_STOCK;
  }

  if (/only\s+\d+\s+(?:left|remaining|in stock)/.test(text)) {
    return STOCK.QUANTITY;
  }

  if (/\bin stock\b|available for shipping|shipping available/.test(text)) {
    return STOCK.IN_STOCK;
  }

  return STOCK.UNKNOWN;
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => {
      data += chunk;
    });
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

function readOptionalConfig(configPath) {
  const defaults = {
    navigation_timeout_ms: 45000,
    settle_timeout_ms: 10000,
    process_timeout_seconds: 90,
    save_debug_evidence: true,
    screenshots_on_error: true,
    close_scrape_tab_after_check: true,
  };

  try {
    const parsed = JSON.parse(
      fs.readFileSync(configPath, "utf8")
    );
    return {
      ...defaults,
      ...parsed,
    };
  } catch {
    return defaults;
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);

  if (!response.ok) {
    throw new Error(
      `Brave CDP request failed (${response.status}): ${url}`
    );
  }

  return response.json();
}

class CdpClient {
  constructor(webSocketUrl) {
    this.webSocketUrl = webSocketUrl;
    this.socket = null;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
  }

  async connect(timeoutMs = 10000) {
    await new Promise((resolve, reject) => {
      const socket = new WebSocket(this.webSocketUrl);
      this.socket = socket;

      const timer = setTimeout(() => {
        reject(
          new Error(
            `Timed out connecting to Brave CDP: ${this.webSocketUrl}`
          )
        );
      }, timeoutMs);

      socket.addEventListener("open", () => {
        clearTimeout(timer);
        resolve();
      });

      socket.addEventListener("error", event => {
        clearTimeout(timer);
        reject(
          new Error(
            `Brave CDP WebSocket error: ${event.message || "connection failed"}`
          )
        );
      });

      socket.addEventListener("message", event => {
        this.handleMessage(event.data);
      });

      socket.addEventListener("close", () => {
        for (const pending of this.pending.values()) {
          pending.reject(
            new Error("Brave CDP connection closed.")
          );
        }
        this.pending.clear();
      });
    });
  }

  handleMessage(raw) {
    let message;

    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }

    if (message.id && this.pending.has(message.id)) {
      const pending = this.pending.get(message.id);
      this.pending.delete(message.id);

      if (message.error) {
        pending.reject(
          new Error(
            `${pending.method}: ${message.error.message || "CDP error"}`
          )
        );
      } else {
        pending.resolve(message.result || {});
      }
      return;
    }

    if (message.method) {
      const listeners = this.listeners.get(message.method) || [];

      for (const listener of listeners) {
        try {
          listener(
            message.params || {},
            message.sessionId || ""
          );
        } catch {
          // An evidence listener must never stop the scrape.
        }
      }
    }
  }

  on(method, listener) {
    const listeners = this.listeners.get(method) || [];
    listeners.push(listener);
    this.listeners.set(method, listeners);

    return () => {
      const current = this.listeners.get(method) || [];
      this.listeners.set(
        method,
        current.filter(item => item !== listener)
      );
    };
  }

  send(method, params = {}, sessionId = "", timeoutMs = 20000) {
    const id = this.nextId;
    this.nextId += 1;

    const payload = {
      id,
      method,
      params,
    };

    if (sessionId) {
      payload.sessionId = sessionId;
    }

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(
          new Error(`${method} timed out after ${timeoutMs} ms.`)
        );
      }, timeoutMs);

      this.pending.set(id, {
        method,
        resolve: result => {
          clearTimeout(timer);
          resolve(result);
        },
        reject: error => {
          clearTimeout(timer);
          reject(error);
        },
      });

      this.socket.send(JSON.stringify(payload));
    });
  }

  close() {
    try {
      this.socket?.close();
    } catch {
      // The user's Brave browser must never be closed.
    }
  }
}

async function evaluate(
  client,
  sessionId,
  expression,
  timeoutMs = 20000
) {
  const result = await client.send(
    "Runtime.evaluate",
    {
      expression,
      returnByValue: true,
      awaitPromise: true,
      userGesture: false,
    },
    sessionId,
    timeoutMs
  );

  if (result.exceptionDetails) {
    const description =
      result.exceptionDetails.exception?.description ||
      result.exceptionDetails.text ||
      "Page evaluation failed.";

    throw new Error(description);
  }

  return result.result?.value;
}

function objectItemIds(object) {
  const result = [];

  for (const key of [
    "usItemId",
    "itemId",
    "item_id",
    "productId",
    "product_id",
    "offerId",
  ]) {
    const value = object?.[key];

    if (value !== null && value !== undefined) {
      result.push(String(value));
    }
  }

  return result;
}

function findExactObjects(root, itemId) {
  const matches = [];
  const seen = new WeakSet();
  const stack = [{
    value: root,
    path: "$",
    depth: 0,
  }];

  while (stack.length) {
    const current = stack.pop();
    const value = current.value;

    if (!value || typeof value !== "object") {
      continue;
    }

    if (seen.has(value)) {
      continue;
    }

    seen.add(value);

    if (
      !Array.isArray(value) &&
      objectItemIds(value).includes(itemId)
    ) {
      matches.push({
        object: value,
        path: current.path,
        depth: current.depth,
      });
    }

    if (current.depth >= 15) {
      continue;
    }

    if (Array.isArray(value)) {
      const limit = Math.min(value.length, 400);

      for (let index = 0; index < limit; index += 1) {
        stack.push({
          value: value[index],
          path: `${current.path}[${index}]`,
          depth: current.depth + 1,
        });
      }
    } else {
      const entries = Object.entries(value);
      const limit = Math.min(entries.length, 400);

      for (let index = 0; index < limit; index += 1) {
        const [key, child] = entries[index];

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

function primitiveEntries(object, maxDepth = 5) {
  const entries = [];
  const seen = new WeakSet();
  const stack = [{
    value: object,
    key: "",
    depth: 0,
  }];

  while (stack.length) {
    const current = stack.pop();
    const value = current.value;

    if (
      value === null ||
      value === undefined
    ) {
      continue;
    }

    if (
      typeof value === "string" ||
      typeof value === "number" ||
      typeof value === "boolean"
    ) {
      entries.push({
        key: current.key,
        value,
      });
      continue;
    }

    if (
      typeof value !== "object" ||
      seen.has(value) ||
      current.depth >= maxDepth
    ) {
      continue;
    }

    seen.add(value);

    if (Array.isArray(value)) {
      for (const child of value.slice(0, 100)) {
        stack.push({
          value: child,
          key: current.key,
          depth: current.depth + 1,
        });
      }
    } else {
      for (
        const [key, child]
        of Object.entries(value).slice(0, 200)
      ) {
        stack.push({
          value: child,
          key,
          depth: current.depth + 1,
        });
      }
    }
  }

  return entries;
}

function structuredCandidate(match) {
  const entries = primitiveEntries(match.object);

  let title = "";
  let price = "";
  let stock = STOCK.UNKNOWN;
  let seller = "";
  let selectedScore = 0;

  for (const entry of entries) {
    const key = String(entry.key || "");
    const value = entry.value;

    if (
      !title &&
      /^(name|title|productName|productTitle)$/i.test(key) &&
      typeof value === "string"
    ) {
      const text = normalizeText(value);

      if (text.length >= 3 && text.length <= 300) {
        title = text;
      }
    }

    if (
      !price &&
      /(currentPrice|price|priceValue|linePrice|itemPrice|displayPrice)/i.test(key)
    ) {
      price = normalizePrice(value);
    }

    if (
      stock === STOCK.UNKNOWN &&
      /(availabilityStatus|availability|stockStatus|inventoryStatus|offerAvailability|fulfillmentStatus)/i.test(key)
    ) {
      stock = stockFromText(value);
    }

    if (
      !seller &&
      /(sellerName|sellerDisplayName|seller|soldBy)/i.test(key) &&
      typeof value === "string"
    ) {
      seller = normalizeText(value);
    }

    if (
      /(selected|isSelected|currentSelection)/i.test(key) &&
      (
        value === true ||
        String(value).toLowerCase() === "true"
      )
    ) {
      selectedScore += 5;
    }
  }

  if (stock === STOCK.UNKNOWN) {
    const serialized = normalizeText(
      JSON.stringify(match.object)
    ).slice(0, 12000);

    stock = stockFromText(serialized);
  }

  let score = 100;

  if (title) score += 10;
  if (price) score += 20;
  if (stock !== STOCK.UNKNOWN) score += 30;
  if (seller) score += 5;

  score += selectedScore;
  score -= Math.min(match.depth, 20);

  if (/recommend|sponsor|carousel|similar|related/i.test(match.path)) {
    score -= 60;
  }

  if (/buyBox|purchase|offer|product|item/i.test(match.path)) {
    score += 12;
  }

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
    for (const match of findExactObjects(body.json, itemId)) {
      const candidate = structuredCandidate(match);
      candidate.responseUrl = body.url;
      candidates.push(candidate);
    }
  }

  candidates.sort((first, second) => second.score - first.score);

  return {
    best: candidates[0] || null,
    candidates: candidates.slice(0, 8),
  };
}

function pageEvidenceExpression(itemId) {
  return `(() => {
    const requestedItemId = ${JSON.stringify(itemId)};
    const normalize = value =>
      String(value || "").replace(/\\s+/g, " ").trim();

    const visible = element => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();

      return (
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        Number(style.opacity || 1) > 0 &&
        rect.width > 0 &&
        rect.height > 0
      );
    };

    const pageY = element => {
      const rect = element.getBoundingClientRect();
      return rect.top + scrollY;
    };

    const tokenAncestor = (element, tokens, depth = 9) => {
      let current = element;

      for (let index = 0; current && index < depth; index += 1) {
        const token = [
          current.id || "",
          current.className || "",
          current.getAttribute?.("data-testid") || "",
          current.getAttribute?.("data-automation-id") || "",
          current.getAttribute?.("aria-label") || "",
        ].join(" ").toLowerCase();

        if (tokens.some(item => token.includes(item))) {
          return true;
        }

        current = current.parentElement;
      }

      return false;
    };

    const recommendationTokens = [
      "recommend",
      "sponsor",
      "carousel",
      "similar",
      "related",
      "also-viewed",
      "popular-picks",
    ];

    const main =
      document.querySelector("main") ||
      document.querySelector('[role="main"]') ||
      document.body;

    const h1 = [...main.querySelectorAll("h1")]
      .filter(visible)[0] || null;

    const title = normalize(
      h1?.textContent ||
      document.title
    );

    const anchorY = h1
      ? pageY(h1) + h1.getBoundingClientRect().height
      : Math.max(scrollY, 0);

    const buttons = [...main.querySelectorAll("button")]
      .filter(visible)
      .filter(button =>
        /add to cart|add to basket/i.test(
          normalize(button.textContent)
        )
      )
      .filter(button => {
        const y = pageY(button);

        return (
          y >= anchorY - 250 &&
          y <= anchorY + 1900 &&
          !tokenAncestor(
            button,
            recommendationTokens,
            10
          )
        );
      })
      .sort((first, second) => {
        const firstDistance = Math.abs(pageY(first) - anchorY);
        const secondDistance = Math.abs(pageY(second) - anchorY);
        return firstDistance - secondDistance;
      });

    const enabledAdd = buttons.find(button =>
      !button.disabled &&
      button.getAttribute("aria-disabled") !== "true"
    ) || null;

    const disabledAdd = buttons.find(button =>
      button.disabled ||
      button.getAttribute("aria-disabled") === "true"
    ) || null;

    const chosenButton = enabledAdd || disabledAdd;
    const chosenY = chosenButton
      ? pageY(chosenButton)
      : anchorY + 600;

    const priceMeta =
      document.querySelector('meta[itemprop="price"]')?.content ||
      document.querySelector('meta[property="product:price:amount"]')?.content ||
      "";

    let priceText = normalize(priceMeta);

    if (!priceText) {
      const priceCandidates = [
        ...main.querySelectorAll(
          '[data-testid*="price"],' +
          '[data-automation-id*="price"],' +
          '[itemprop="price"]'
        ),
      ]
        .filter(visible)
        .map(element => ({
          text: normalize(
            element.getAttribute("content") ||
            element.textContent
          ),
          y: pageY(element),
          recommendation: tokenAncestor(
            element,
            recommendationTokens,
            10
          ),
        }))
        .filter(item =>
          !item.recommendation &&
          /\\$\\s*\\d/.test(item.text) &&
          item.y >= anchorY - 250 &&
          item.y <= anchorY + 1400
        )
        .sort((first, second) =>
          Math.abs(first.y - anchorY) -
          Math.abs(second.y - anchorY)
        );

      priceText = priceCandidates[0]?.text || "";
    }

    const atomicTexts = [
      ...main.querySelectorAll("span, p, div, button"),
    ]
      .filter(visible)
      .filter(element =>
        !tokenAncestor(
          element,
          recommendationTokens,
          10
        )
      )
      .map(element => {
        const direct = normalize(
          [...element.childNodes]
            .filter(node => node.nodeType === Node.TEXT_NODE)
            .map(node => node.textContent)
            .join(" ")
        );

        return {
          text: direct || (
            element.children.length === 0
              ? normalize(element.textContent)
              : ""
          ),
          y: pageY(element),
        };
      })
      .filter(item =>
        item.text &&
        item.text.length <= 180
      );

    const exactStockTexts = atomicTexts
      .filter(item =>
        item.y >= anchorY - 120 &&
        item.y <= chosenY + 500 &&
        /low stock|limited stock|only\\s+\\d+\\s+(?:left|remaining|in stock)|out of stock|sold out|back[\\s-]?order|currently unavailable/i.test(item.text)
      )
      .sort((first, second) =>
        Math.abs(first.y - chosenY) -
        Math.abs(second.y - chosenY)
      )
      .map(item => item.text)
      .filter((value, index, array) =>
        array.indexOf(value) === index
      )
      .slice(0, 12);

    const fulfillment = {};

    for (const label of ["shipping", "pickup", "delivery"]) {
      const labelNodes = [
        ...main.querySelectorAll("span, p, div, h3, h4"),
      ]
        .filter(visible)
        .filter(element =>
          normalize(element.textContent).toLowerCase() === label
        )
        .filter(element => {
          const y = pageY(element);

          return (
            y >= anchorY - 100 &&
            y <= chosenY + 900 &&
            !tokenAncestor(
              element,
              recommendationTokens,
              10
            )
          );
        });

      let best = null;

      for (const labelNode of labelNodes) {
        let current = labelNode.parentElement;

        for (
          let depth = 0;
          current && depth < 7;
          depth += 1, current = current.parentElement
        ) {
          if (!visible(current)) continue;

          const text = normalize(current.textContent);

          if (
            !text ||
            text.length > 550
          ) {
            continue;
          }

          const otherLabels = [
            "shipping",
            "pickup",
            "delivery",
          ]
            .filter(other => other !== label)
            .filter(other =>
              new RegExp("\\\\b" + other + "\\\\b", "i").test(text)
            )
            .length;

          const candidate = {
            text,
            otherLabels,
            depth,
          };

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

      if (best) {
        fulfillment[label] = best.text;
      }
    }

    const embeddedJsonTexts = [];
    let embeddedTotal = 0;

    for (
      const script of document.querySelectorAll(
        'script#__NEXT_DATA__, script[type="application/json"]'
      )
    ) {
      const text = script.textContent || "";

      if (
        !text.includes(requestedItemId) ||
        text.length > 2500000 ||
        embeddedTotal + text.length > 5000000
      ) {
        continue;
      }

      embeddedJsonTexts.push(text);
      embeddedTotal += text.length;

      if (embeddedJsonTexts.length >= 12) {
        break;
      }
    }

    const bodyText = normalize(
      document.body?.innerText || ""
    ).slice(0, 20000);

    return {
      title,
      priceText,
      enabledAdd: Boolean(enabledAdd),
      disabledAdd: Boolean(disabledAdd),
      exactStockTexts,
      fulfillment,
      embeddedJsonTexts,
      bodyText,
      pageUrl: location.href,
    };
  })()`;
}

function domStock(dom) {
  for (const text of dom.exactStockTexts || []) {
    const state = stockFromText(text);

    if (
      state === STOCK.LOW_STOCK ||
      state === STOCK.LIMITED_STOCK ||
      state === STOCK.QUANTITY ||
      state === STOCK.OOS
    ) {
      return {
        stock: state,
        evidence: text,
      };
    }
  }

  if (dom.enabledAdd) {
    return {
      stock: STOCK.IN_STOCK,
      evidence: "Enabled primary Add to cart",
    };
  }

  if (dom.disabledAdd) {
    return {
      stock: STOCK.OOS,
      evidence: "Disabled primary Add to cart",
    };
  }

  const fulfillment = Object.entries(
    dom.fulfillment || {}
  ).map(([method, text]) => ({
    method,
    text,
    stock: stockFromText(text),
  }));

  const available = fulfillment.filter(
    item => item.stock === STOCK.IN_STOCK
  );

  if (available.length) {
    return {
      stock: STOCK.IN_STOCK,
      evidence: available
        .map(item => `${item.method}: ${item.text}`)
        .join(" | "),
    };
  }

  if (
    fulfillment.length >= 2 &&
    fulfillment.every(item => item.stock === STOCK.OOS)
  ) {
    return {
      stock: STOCK.OOS,
      evidence: fulfillment
        .map(item => `${item.method}: ${item.text}`)
        .join(" | "),
    };
  }

  return {
    stock: STOCK.UNKNOWN,
    evidence: "",
  };
}

function finalDecision(structured, dom) {
  const domResult = domStock(dom);
  const structuredStock =
    structured?.stock || STOCK.UNKNOWN;

  if (
    domResult.stock === STOCK.LOW_STOCK ||
    domResult.stock === STOCK.LIMITED_STOCK ||
    domResult.stock === STOCK.QUANTITY
  ) {
    return {
      stock: domResult.stock,
      reason:
        `The exact linked item's selected offer reports: ` +
        `${domResult.evidence}.`,
    };
  }

  if (
    domResult.stock === STOCK.OOS &&
    !dom.enabledAdd
  ) {
    return {
      stock: STOCK.OOS,
      reason:
        `The exact linked item's selected offer reports OOS: ` +
        `${domResult.evidence}.`,
    };
  }

  if (
    domResult.stock === STOCK.IN_STOCK &&
    dom.enabledAdd
  ) {
    return {
      stock: STOCK.IN_STOCK,
      reason:
        "The exact linked item's primary offer has an enabled " +
        "Add to cart control.",
    };
  }

  if (
    structuredStock !== STOCK.UNKNOWN &&
    domResult.stock === STOCK.UNKNOWN
  ) {
    return {
      stock: structuredStock,
      reason:
        "Exact-item structured Walmart data supplied the stock result.",
    };
  }

  if (
    structuredStock === STOCK.UNKNOWN &&
    domResult.stock !== STOCK.UNKNOWN
  ) {
    return {
      stock: domResult.stock,
      reason:
        "The exact linked item's primary purchase area supplied " +
        `the stock result: ${domResult.evidence}.`,
    };
  }

  if (
    structuredStock === domResult.stock &&
    structuredStock !== STOCK.UNKNOWN
  ) {
    return {
      stock: structuredStock,
      reason:
        "Exact-item structured data and the selected purchase " +
        "area agree.",
    };
  }

  return {
    stock: STOCK.UNKNOWN,
    reason:
      "The exact Walmart item did not provide one reliable stock " +
      `state (${structuredStock} versus ${domResult.stock}).`,
  };
}

async function waitForPage(
  client,
  sessionId,
  state,
  navigationTimeoutMs,
  settleTimeoutMs
) {
  let loaded = false;
  let lastActivity = Date.now();

  const removeLoad = client.on(
    "Page.loadEventFired",
    (_params, eventSessionId) => {
      if (eventSessionId === sessionId) {
        loaded = true;
        lastActivity = Date.now();
      }
    }
  );

  const networkMethods = [
    "Network.requestWillBeSent",
    "Network.loadingFinished",
    "Network.loadingFailed",
  ];

  const removers = networkMethods.map(method =>
    client.on(method, (_params, eventSessionId) => {
      if (eventSessionId === sessionId) {
        lastActivity = Date.now();
      }
    })
  );

  const start = Date.now();

  try {
    while (Date.now() - start < navigationTimeoutMs) {
      if (
        loaded &&
        Date.now() - lastActivity >= 1500
      ) {
        return;
      }

      await sleep(150);
    }

    if (!loaded) {
      throw new Error(
        `Walmart page did not finish loading within ` +
        `${navigationTimeoutMs} ms.`
      );
    }

    await sleep(Math.min(settleTimeoutMs, 2500));
  } finally {
    removeLoad();
    for (const remove of removers) {
      remove();
    }
  }
}

function safeFileName(value) {
  return String(value)
    .replace(/[^a-z0-9._-]+/gi, "_")
    .slice(0, 120);
}

async function main() {
  const request = await readStdin();
  const url = normalizeText(request.url);
  const projectRoot = path.resolve(
    request.projectRoot || process.cwd()
  );
  const configPath = path.resolve(
    request.configPath ||
    path.join(
      projectRoot,
      "config",
      "walmart_playwright.json"
    )
  );
  const config = readOptionalConfig(configPath);
  const cdpUrl =
    process.env.WALMART_CDP_URL ||
    "http://127.0.0.1:9222";
  const itemId = exactItemId(url);

  if (
    !url ||
    !/^https?:\/\/(?:www\.)?walmart\.com\//i.test(url)
  ) {
    throw new Error(
      "The supplied link is not a Walmart product URL."
    );
  }

  if (!itemId) {
    throw new Error(
      "Could not identify the exact Walmart item ID from the link."
    );
  }

  const version = await fetchJson(
    `${cdpUrl}/json/version`
  );

  if (!version.webSocketDebuggerUrl) {
    throw new Error(
      "Brave debugging is running, but it did not provide a " +
      "browser WebSocket endpoint."
    );
  }

  const client = new CdpClient(
    version.webSocketDebuggerUrl
  );

  await client.connect(10000);

  let targetId = "";
  let sessionId = "";
  const jsonBodies = [];
  const responseCandidates = new Map();
  const bodyPromises = new Set();

  try {
    const created = await client.send(
      "Target.createTarget",
      {
        url: "about:blank",
      }
    );

    targetId = created.targetId;

    const attached = await client.send(
      "Target.attachToTarget",
      {
        targetId,
        flatten: true,
      }
    );

    sessionId = attached.sessionId;

    await Promise.all([
      client.send(
        "Page.enable",
        {},
        sessionId
      ),
      client.send(
        "Runtime.enable",
        {},
        sessionId
      ),
      client.send(
        "Network.enable",
        {
          maxTotalBufferSize: 50000000,
          maxResourceBufferSize: 10000000,
        },
        sessionId
      ),
    ]);

    client.on(
      "Network.responseReceived",
      (params, eventSessionId) => {
        if (eventSessionId !== sessionId) {
          return;
        }

        const response = params.response || {};
        const responseUrl = String(response.url || "");
        const mimeType = String(response.mimeType || "");

        if (
          /json/i.test(mimeType) ||
          /walmart|graphql|product|item|offer|fulfill|inventory/i.test(
            responseUrl
          )
        ) {
          responseCandidates.set(
            params.requestId,
            {
              url: responseUrl,
              mimeType,
            }
          );
        }
      }
    );

    client.on(
      "Network.loadingFinished",
      (params, eventSessionId) => {
        if (
          eventSessionId !== sessionId ||
          !responseCandidates.has(params.requestId)
        ) {
          return;
        }

        const metadata = responseCandidates.get(
          params.requestId
        );
        responseCandidates.delete(params.requestId);

        const promise = client.send(
          "Network.getResponseBody",
          {
            requestId: params.requestId,
          },
          sessionId,
          12000
        )
          .then(result => {
            let body = result.body || "";

            if (result.base64Encoded) {
              body = Buffer.from(
                body,
                "base64"
              ).toString("utf8");
            }

            if (
              !body ||
              body.length > 10000000
            ) {
              return;
            }

            try {
              jsonBodies.push({
                url: metadata.url,
                json: JSON.parse(body),
              });
            } catch {
              // Non-JSON responses are ignored.
            }
          })
          .catch(() => {})
          .finally(() => {
            bodyPromises.delete(promise);
          });

        bodyPromises.add(promise);
      }
    );

    await client.send(
      "Page.navigate",
      {
        url,
      },
      sessionId,
      config.navigation_timeout_ms
    );

    await waitForPage(
      client,
      sessionId,
      {},
      config.navigation_timeout_ms,
      config.settle_timeout_ms
    );

    await sleep(1200);

    const dom = await evaluate(
      client,
      sessionId,
      pageEvidenceExpression(itemId),
      25000
    );

    if (
      /verify you are human|press and hold|robot or human|access denied|captcha/i.test(
        dom.bodyText || ""
      )
    ) {
      throw new Error(
        "Walmart requested human verification in Brave. " +
        "Complete it in the temporary Walmart tab, then retry."
      );
    }

    for (const text of dom.embeddedJsonTexts || []) {
      try {
        jsonBodies.push({
          url: "embedded://page-json",
          json: JSON.parse(text),
        });
      } catch {
        // Ignore unreadable embedded data.
      }
    }

    await Promise.allSettled(
      [...bodyPromises]
    );

    const finalPageItemId = exactItemId(
      dom.pageUrl
    );

    if (
      finalPageItemId &&
      finalPageItemId !== itemId
    ) {
      throw new Error(
        `Walmart redirected item ${itemId} to a different ` +
        `item ${finalPageItemId}.`
      );
    }

    const structured = bestStructuredEvidence(
      jsonBodies,
      itemId
    );
    const decision = finalDecision(
      structured.best,
      dom
    );

    const title =
      dom.title ||
      structured.best?.title ||
      `Walmart item ${itemId}`;

    // The visible selected-offer price has priority because the user's
    // requirement is the price shown for this exact URL.
    const price =
      normalizePrice(dom.priceText) ||
      structured.best?.price ||
      "";

    const result = {
      status:
        decision.stock === STOCK.UNKNOWN
          ? "Error"
          : "Success",
      itemId,
      title,
      price:
        decision.stock === STOCK.UNKNOWN
          ? ""
          : price,
      stock: decision.stock,
      reason: decision.reason,
      evidence: {
        runtimeVersion: VERSION,
        browserMode: "brave_cdp_raw",
        requestedItemId: itemId,
        pageUrl: dom.pageUrl,
        structuredBest: structured.best,
        structuredCandidates: structured.candidates,
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

    const evidenceDir = path.join(
      projectRoot,
      "logs",
      "walmart_evidence"
    );

    if (config.save_debug_evidence !== false) {
      fs.mkdirSync(
        evidenceDir,
        {
          recursive: true,
        }
      );

      const stamp = new Date()
        .toISOString()
        .replace(/[:.]/g, "-");

      fs.writeFileSync(
        path.join(
          evidenceDir,
          `${stamp}_${safeFileName(itemId)}.json`
        ),
        JSON.stringify(result, null, 2),
        "utf8"
      );
    }

    process.stdout.write(
      JSON.stringify(result)
    );
  } finally {
    if (targetId) {
      await client.send(
        "Target.closeTarget",
        {
          targetId,
        },
        "",
        8000
      ).catch(() => {});
    }

    client.close();
  }
}

function selfTest() {
  const checks = [
    [
      exactItemId(
        "https://www.walmart.com/ip/Test/4404150"
      ),
      "4404150",
    ],
    [
      stockFromText("Low stock"),
      STOCK.LOW_STOCK,
    ],
    [
      stockFromText("Only 3 remaining"),
      STOCK.QUANTITY,
    ],
    [
      stockFromText("Out of stock"),
      STOCK.OOS,
    ],
    [
      normalizePrice("$1,299.99"),
      "1299.99",
    ],
  ];

  for (const [actual, expected] of checks) {
    if (actual !== expected) {
      throw new Error(
        `Self-test failed: expected ${expected}, received ${actual}`
      );
    }
  }

  process.stdout.write(
    "Walmart pure Brave CDP runtime self-test passed.\n"
  );
}

if (process.argv.includes("--self-test")) {
  try {
    selfTest();
  } catch (error) {
    process.stderr.write(
      String(error?.stack || error)
    );
    process.exitCode = 1;
  }
} else {
  main().catch(error => {
    process.stderr.write(
      String(
        error?.stack ||
        error?.message ||
        error
      )
    );
    process.exitCode = 1;
  });
}
