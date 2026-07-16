import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const VERSION = "2026.07.16.variant-offer-v7";

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
  const original = normalizeText(value);

  const text = original
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/[_/.:?-]+/g, " ")
    .replace(/\s+/g, " ")
    .toLowerCase()
    .trim();

  if (!text) {
    return STOCK.UNKNOWN;
  }

  if (
    /back[\s-]?order|sold out|out of stock|currently unavailable|item unavailable|not available|unavailable for (?:shipping|pickup|delivery)|schema org out of stock/.test(
      text
    )
  ) {
    return STOCK.OOS;
  }

  if (/limited stock/.test(text)) {
    return STOCK.LIMITED_STOCK;
  }

  if (/low stock|few left/.test(text)) {
    return STOCK.LOW_STOCK;
  }

  if (
    /only\s+\d+\s+(?:left|remaining|in stock)|\d+\s+left in stock/.test(
      text
    )
  ) {
    return STOCK.QUANTITY;
  }

  if (
    /\bin stock\b|available for shipping|shipping available|available for delivery|delivery available|available for pickup|pickup available|schema org in stock/.test(
      text
    )
  ) {
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
    "sku",
    "productID",
    "productId",
    "product_id",
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

    const tokenAncestor = (element, tokens, depth = 10) => {
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
      "you-may-also-like",
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

    const allControls = [
      ...main.querySelectorAll(
        'button, [role="button"], input[type="button"], input[type="submit"], a'
      ),
    ]
      .filter(visible)
      .filter(control =>
        !tokenAncestor(
          control,
          recommendationTokens,
          10
        )
      )
      .map(control => {
        const label = normalize(
          control.getAttribute("aria-label") ||
          control.getAttribute("title") ||
          control.value ||
          control.textContent
        );

        const token = normalize([
          control.getAttribute("data-testid") || "",
          control.getAttribute("data-automation-id") || "",
          control.id || "",
          control.className || "",
        ].join(" "));

        return {
          element: control,
          label,
          token,
          y: pageY(control),
          enabled:
            !control.disabled &&
            control.getAttribute("aria-disabled") !== "true",
        };
      })
      .filter(item =>
        item.y >= anchorY - 300 &&
        item.y <= anchorY + 2500
      );

    const cartControls = allControls
      .filter(item =>
        /add to cart|add to basket|add item|add$|add to order/i.test(
          item.label + " " + item.token
        ) ||
        /add-to-cart|addtocart|add_to_cart/i.test(item.token)
      )
      .sort((first, second) =>
        Math.abs(first.y - anchorY) -
        Math.abs(second.y - anchorY)
      );

    const enabledAdd =
      cartControls.find(item => item.enabled) || null;
    const disabledAdd =
      cartControls.find(item => !item.enabled) || null;
    const chosenControl = enabledAdd || disabledAdd;
    const chosenY = chosenControl
      ? chosenControl.y
      : anchorY + 800;

    const buyNowControl = allControls.find(item =>
      item.enabled &&
      /buy now|checkout now/i.test(
        item.label + " " + item.token
      )
    ) || null;

    const offerControls = allControls
      .filter(item =>
        /see all buying options|more seller options|choose options|select options|check availability|see options|add to cart/i.test(
          item.label + " " + item.token
        )
      )
      .slice(0, 15)
      .map(item => ({
        label: item.label,
        token: item.token,
        enabled: item.enabled,
        y: item.y,
      }));

    const availabilityMeta = normalize(
      document.querySelector('link[itemprop="availability"]')?.href ||
      document.querySelector('meta[itemprop="availability"]')?.content ||
      document.querySelector('meta[property="product:availability"]')?.content ||
      ""
    );

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
          '[itemprop="price"],' +
          '[aria-label*="$"]'
        ),
      ]
        .filter(visible)
        .map(element => ({
          text: normalize(
            element.getAttribute("content") ||
            element.getAttribute("aria-label") ||
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
          item.y >= anchorY - 300 &&
          item.y <= anchorY + 1800
        )
        .sort((first, second) =>
          Math.abs(first.y - anchorY) -
          Math.abs(second.y - anchorY)
        );

      priceText = priceCandidates[0]?.text || "";
    }

    const atomicTexts = [
      ...main.querySelectorAll("span, p, div, button, li"),
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
        item.text.length <= 240
      );

    const exactStockTexts = atomicTexts
      .filter(item =>
        item.y >= anchorY - 180 &&
        item.y <= chosenY + 750 &&
        /low stock|limited stock|few left|only\\s+\\d+\\s+(?:left|remaining|in stock)|\\d+\\s+left in stock|out of stock|sold out|back[\\s-]?order|currently unavailable|item unavailable|not available|available for shipping|available for delivery|available for pickup|shipping available|delivery available|pickup available/i.test(item.text)
      )
      .sort((first, second) =>
        Math.abs(first.y - chosenY) -
        Math.abs(second.y - chosenY)
      )
      .map(item => item.text)
      .filter((value, index, array) =>
        array.indexOf(value) === index
      )
      .slice(0, 20);

    const purchaseTexts = atomicTexts
      .filter(item =>
        item.y >= anchorY - 180 &&
        item.y <= chosenY + 1100
      )
      .map(item => item.text)
      .filter(text =>
        /sold and shipped by|fulfilled by|shipping|pickup|delivery|arrives|available|unavailable|in stock|out of stock|add to cart|buy now|seller/i.test(text)
      )
      .filter((value, index, array) =>
        array.indexOf(value) === index
      )
      .slice(0, 40);

    const fulfillment = {};

    for (const label of ["shipping", "pickup", "delivery"]) {
      const labelNodes = [
        ...main.querySelectorAll("span, p, div, h3, h4, button"),
      ]
        .filter(visible)
        .filter(element =>
          normalize(element.textContent).toLowerCase() === label
        )
        .filter(element => {
          const y = pageY(element);

          return (
            y >= anchorY - 150 &&
            y <= chosenY + 1200 &&
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
          current && depth < 8;
          depth += 1, current = current.parentElement
        ) {
          if (!visible(current)) continue;

          const text = normalize(current.textContent);

          if (
            !text ||
            text.length > 750
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

    const selectedVariantSignals = [
      ...main.querySelectorAll(
        '[aria-checked="true"], [aria-selected="true"], [data-selected="true"], [data-state="selected"]'
      ),
    ]
      .filter(visible)
      .map(element => normalize(
        element.getAttribute("aria-label") ||
        element.getAttribute("title") ||
        element.textContent
      ))
      .filter(Boolean)
      .filter((value, index, array) =>
        array.indexOf(value) === index
      )
      .slice(0, 20);

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
        text.length > 3000000 ||
        embeddedTotal + text.length > 7000000
      ) {
        continue;
      }

      embeddedJsonTexts.push(text);
      embeddedTotal += text.length;

      if (embeddedJsonTexts.length >= 16) {
        break;
      }
    }

    const jsonLdTexts = [];

    for (
      const script of document.querySelectorAll(
        'script[type="application/ld+json"]'
      )
    ) {
      const text = script.textContent || "";

      if (
        !text ||
        text.length > 1500000
      ) {
        continue;
      }

      jsonLdTexts.push(text);

      if (jsonLdTexts.length >= 12) {
        break;
      }
    }

    const bodyText = normalize(
      document.body?.innerText || ""
    ).slice(0, 30000);

    return {
      title,
      priceText,
      availabilityMeta,
      enabledAdd: Boolean(enabledAdd),
      disabledAdd: Boolean(disabledAdd),
      buyNowEnabled: Boolean(buyNowControl),
      cartControlLabel:
        enabledAdd?.label ||
        disabledAdd?.label ||
        "",
      cartControlToken:
        enabledAdd?.token ||
        disabledAdd?.token ||
        "",
      offerControls,
      exactStockTexts,
      purchaseTexts,
      fulfillment,
      selectedVariantSignals,
      embeddedJsonTexts,
      jsonLdTexts,
      bodyText,
      pageUrl: location.href,
    };
  })()`;
}

function domStock(dom) {
  const availabilityMetaState = stockFromText(
    dom.availabilityMeta || ""
  );

  if (
    availabilityMetaState !== STOCK.UNKNOWN
  ) {
    return {
      stock: availabilityMetaState,
      evidence:
        `Availability metadata: ${dom.availabilityMeta}`,
      strength: "strong",
    };
  }

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
        strength: "strong",
      };
    }
  }

  if (
    dom.enabledAdd ||
    dom.buyNowEnabled
  ) {
    return {
      stock: STOCK.IN_STOCK,
      evidence:
        dom.enabledAdd
          ? `Enabled purchase control: ${dom.cartControlLabel || "Add to cart"}`
          : "Enabled Buy now control",
      strength: "strong",
    };
  }

  if (
    dom.disabledAdd &&
    !(dom.offerControls || []).some(
      item => item.enabled &&
      /see all buying options|more seller options|see options/i.test(
        item.label + " " + item.token
      )
    )
  ) {
    return {
      stock: STOCK.OOS,
      evidence:
        `Disabled purchase control: ${dom.cartControlLabel || "Add to cart"}`,
      strength: "medium",
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
      strength: "medium",
    };
  }

  const unavailable = fulfillment.filter(
    item => item.stock === STOCK.OOS
  );

  if (
    fulfillment.length >= 2 &&
    unavailable.length === fulfillment.length
  ) {
    return {
      stock: STOCK.OOS,
      evidence: fulfillment
        .map(item => `${item.method}: ${item.text}`)
        .join(" | "),
      strength: "medium",
    };
  }

  for (const text of dom.purchaseTexts || []) {
    const state = stockFromText(text);

    if (state !== STOCK.UNKNOWN) {
      return {
        stock: state,
        evidence: text,
        strength: "weak",
      };
    }
  }

  return {
    stock: STOCK.UNKNOWN,
    evidence: "",
    strength: "none",
  };
}

function structuredConsensus(structured) {
  const candidates = (
    structured?.candidates || []
  ).filter(candidate =>
    candidate.stock &&
    candidate.stock !== STOCK.UNKNOWN &&
    candidate.score >= 115
  );

  if (!candidates.length) {
    return {
      stock: STOCK.UNKNOWN,
      count: 0,
      evidence: "",
    };
  }

  const counts = new Map();

  for (const candidate of candidates) {
    counts.set(
      candidate.stock,
      (counts.get(candidate.stock) || 0) + 1
    );
  }

  const ordered = [...counts.entries()]
    .sort((first, second) =>
      second[1] - first[1]
    );

  const [stock, count] = ordered[0];

  if (
    count >= 2 ||
    (
      count === 1 &&
      candidates[0]?.score >= 150
    )
  ) {
    return {
      stock,
      count,
      evidence:
        `${count} exact-item structured candidate(s) support ${stock}`,
    };
  }

  return {
    stock: STOCK.UNKNOWN,
    count,
    evidence: "",
  };
}

function finalDecision(structured, dom) {
  const domResult = domStock(dom);
  const consensus = structuredConsensus(structured);
  const structuredBestStock =
    structured?.best?.stock || STOCK.UNKNOWN;
  const structuredStock =
    consensus.stock !== STOCK.UNKNOWN
      ? consensus.stock
      : structuredBestStock;

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
      domResult,
      consensus,
    };
  }

  if (
    domResult.stock === STOCK.OOS &&
    domResult.strength === "strong"
  ) {
    return {
      stock: STOCK.OOS,
      reason:
        `The exact linked item's selected offer reports OOS: ` +
        `${domResult.evidence}.`,
      domResult,
      consensus,
    };
  }

  if (
    domResult.stock === STOCK.IN_STOCK &&
    domResult.strength === "strong"
  ) {
    return {
      stock: STOCK.IN_STOCK,
      reason:
        `The exact linked item's primary purchase area reports ` +
        `In Stock: ${domResult.evidence}.`,
      domResult,
      consensus,
    };
  }

  if (
    structuredStock !== STOCK.UNKNOWN &&
    domResult.stock === STOCK.UNKNOWN
  ) {
    return {
      stock: structuredStock,
      reason:
        consensus.stock !== STOCK.UNKNOWN
          ? `Exact-item Walmart data agrees across ${consensus.count} candidate(s).`
          : "A high-confidence exact-item Walmart offer supplied the stock result.",
      domResult,
      consensus,
    };
  }

  if (
    structuredStock === STOCK.UNKNOWN &&
    domResult.stock !== STOCK.UNKNOWN
  ) {
    return {
      stock: domResult.stock,
      reason:
        "The exact linked item's purchase and fulfillment area supplied " +
        `the stock result: ${domResult.evidence}.`,
      domResult,
      consensus,
    };
  }

  if (
    structuredStock === domResult.stock &&
    structuredStock !== STOCK.UNKNOWN
  ) {
    return {
      stock: structuredStock,
      reason:
        "Exact-item Walmart offer data and the selected purchase area agree.",
      domResult,
      consensus,
    };
  }

  if (
    domResult.stock !== STOCK.UNKNOWN &&
    domResult.strength === "medium" &&
    structuredStock !== STOCK.UNKNOWN &&
    domResult.stock !== structuredStock
  ) {
    return {
      stock: STOCK.UNKNOWN,
      reason:
        "Walmart's selected purchase area and exact-item offer data disagree " +
        `(${domResult.stock} versus ${structuredStock}).`,
      domResult,
      consensus,
    };
  }

  return {
    stock: STOCK.UNKNOWN,
    reason:
      "The exact Walmart item did not provide one reliable stock " +
      `state (${structuredStock} versus ${domResult.stock}).`,
    domResult,
    consensus,
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

function scraperTabStatePath(projectRoot) {
  return path.join(
    projectRoot,
    "logs",
    "walmart_runtime_state.json"
  );
}

function readScraperTabState(projectRoot) {
  const statePath = scraperTabStatePath(projectRoot);

  try {
    const parsed = JSON.parse(
      fs.readFileSync(statePath, "utf8")
    );

    return {
      statePath,
      targetId: normalizeText(parsed.targetId),
    };
  } catch {
    return {
      statePath,
      targetId: "",
    };
  }
}

function writeScraperTabState(
  statePath,
  targetId,
  browserVersion
) {
  fs.mkdirSync(
    path.dirname(statePath),
    {
      recursive: true,
    }
  );

  fs.writeFileSync(
    statePath,
    JSON.stringify(
      {
        targetId,
        browserVersion,
        updatedAt: new Date().toISOString(),
        purpose: "Supplier Stock Checker Walmart scraper tab",
      },
      null,
      2
    ),
    "utf8"
  );
}

function isBlankOrNewTabUrl(value) {
  const url = normalizeText(value).toLowerCase();

  return (
    url === "about:blank" ||
    url.startsWith("chrome://newtab") ||
    url.startsWith("brave://newtab") ||
    url.startsWith("chrome://new-tab-page") ||
    url.startsWith("brave://new-tab-page")
  );
}

async function getOrCreateScraperTarget(
  client,
  projectRoot,
  browserVersion
) {
  const saved = readScraperTabState(projectRoot);
  const targets = await client.send(
    "Target.getTargets"
  );
  const pageTargets = (
    targets.targetInfos || []
  ).filter(target => target.type === "page");

  const saveAndReturn = (
    targetId,
    mode,
    reused
  ) => {
    writeScraperTabState(
      saved.statePath,
      targetId,
      browserVersion
    );

    return {
      targetId,
      mode,
      reused,
      statePath: saved.statePath,
    };
  };

  if (saved.targetId) {
    const existingSaved = pageTargets.find(
      target => target.targetId === saved.targetId
    );

    if (existingSaved) {
      return saveAndReturn(
        existingSaved.targetId,
        "reused_saved_tab",
        true
      );
    }
  }

  // A dedicated Brave debug browser commonly starts with one blank tab.
  // Reusing that tab avoids creating a target that could foreground the
  // browser window.
  if (pageTargets.length === 1) {
    return saveAndReturn(
      pageTargets[0].targetId,
      "adopted_only_existing_tab",
      true
    );
  }

  const blankTarget = pageTargets.find(
    target => isBlankOrNewTabUrl(target.url)
  );

  if (blankTarget) {
    return saveAndReturn(
      blankTarget.targetId,
      "adopted_existing_blank_tab",
      true
    );
  }

  let created;

  try {
    // Chrome CDP explicitly supports focus:false. This creates a normal
    // tab without bringing a background/minimized browser to the front.
    created = await client.send(
      "Target.createTarget",
      {
        url: "about:blank",
        background: false,
        focus: false,
      }
    );
  } catch {
    // Compatibility fallback for Chromium builds without the focus option.
    created = await client.send(
      "Target.createTarget",
      {
        url: "about:blank",
        background: true,
      }
    );
  }

  return saveAndReturn(
    created.targetId,
    "created_without_focus",
    false
  );
}

async function captureWindowProtection(
  client,
  targetId
) {
  try {
    const window = await client.send(
      "Browser.getWindowForTarget",
      {
        targetId,
      }
    );

    return {
      windowId: window.windowId,
      wasMinimized:
        window.bounds?.windowState === "minimized",
    };
  } catch {
    return {
      windowId: null,
      wasMinimized: false,
    };
  }
}

async function preserveMinimizedWindow(
  client,
  protection
) {
  if (
    !protection?.wasMinimized ||
    protection.windowId === null ||
    protection.windowId === undefined
  ) {
    return;
  }

  await client.send(
    "Browser.setWindowBounds",
    {
      windowId: protection.windowId,
      bounds: {
        windowState: "minimized",
      },
    },
    "",
    5000
  ).catch(() => {});
}

function addDomJsonBodies(jsonBodies, dom) {
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

  for (const text of dom.jsonLdTexts || []) {
    try {
      jsonBodies.push({
        url: "embedded://json-ld",
        json: JSON.parse(text),
      });
    } catch {
      // Ignore unreadable JSON-LD.
    }
  }
}

async function triggerPurchaseAreaLoad(
  client,
  sessionId
) {
  await evaluate(
    client,
    sessionId,
    `(() => {
      const normalize = value =>
        String(value || "").replace(/\\s+/g, " ").trim();

      const visible = element => {
        if (!element) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();

        return (
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          rect.width > 0 &&
          rect.height > 0
        );
      };

      const main =
        document.querySelector("main") ||
        document.querySelector('[role="main"]') ||
        document.body;

      const controls = [
        ...main.querySelectorAll(
          'button, [role="button"], input, a'
        ),
      ].filter(visible);

      const purchase = controls.find(element =>
        /add to cart|add to basket|buy now|see all buying options|choose options|select options|check availability/i.test(
          normalize(
            element.getAttribute("aria-label") ||
            element.getAttribute("title") ||
            element.value ||
            element.textContent
          )
        )
      );

      const heading = [
        ...main.querySelectorAll("h1"),
      ].filter(visible)[0];

      const target =
        purchase ||
        heading ||
        main;

      target.scrollIntoView({
        block: "center",
        inline: "nearest",
        behavior: "instant",
      });

      window.dispatchEvent(new Event("scroll"));
      window.dispatchEvent(new Event("resize"));

      return true;
    })()`,
    15000
  ).catch(() => {});
}

async function collectDomPass(
  client,
  sessionId,
  itemId,
  jsonBodies
) {
  const dom = await evaluate(
    client,
    sessionId,
    pageEvidenceExpression(itemId),
    30000
  );

  addDomJsonBodies(
    jsonBodies,
    dom
  );

  return dom;
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
  let scraperTarget = null;
  let windowProtection = null;
  const jsonBodies = [];
  const responseCandidates = new Map();
  const bodyPromises = new Set();

  try {
    scraperTarget = await getOrCreateScraperTarget(
      client,
      projectRoot,
      version.Browser || ""
    );

    targetId = scraperTarget.targetId;
    windowProtection = await captureWindowProtection(
      client,
      targetId
    );

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

    // Some Chromium builds can restore a minimized window during target
    // navigation. Reapply the user's minimized state immediately and again
    // after the page settles.
    await preserveMinimizedWindow(
      client,
      windowProtection
    );

    await waitForPage(
      client,
      sessionId,
      {},
      config.navigation_timeout_ms,
      config.settle_timeout_ms
    );

    await preserveMinimizedWindow(
      client,
      windowProtection
    );

    await sleep(1200);

    await preserveMinimizedWindow(
      client,
      windowProtection
    );

    const attempts = [];

    let dom = await collectDomPass(
      client,
      sessionId,
      itemId,
      jsonBodies
    );

    if (
      /verify you are human|press and hold|robot or human|access denied|captcha/i.test(
        dom.bodyText || ""
      )
    ) {
      throw new Error(
        "Walmart requested human verification in Brave. " +
        "Complete it in the dedicated Walmart tab, then retry."
      );
    }

    await Promise.allSettled(
      [...bodyPromises]
    );

    let structured = bestStructuredEvidence(
      jsonBodies,
      itemId
    );
    let decision = finalDecision(
      structured,
      dom
    );

    attempts.push({
      name: "initial",
      decision,
      dom: {
        enabledAdd: dom.enabledAdd,
        disabledAdd: dom.disabledAdd,
        buyNowEnabled: dom.buyNowEnabled,
        availabilityMeta: dom.availabilityMeta,
        exactStockTexts: dom.exactStockTexts,
        fulfillment: dom.fulfillment,
      },
    });

    if (decision.stock === STOCK.UNKNOWN) {
      await triggerPurchaseAreaLoad(
        client,
        sessionId
      );

      await preserveMinimizedWindow(
        client,
        windowProtection
      );

      await sleep(3000);

      dom = await collectDomPass(
        client,
        sessionId,
        itemId,
        jsonBodies
      );

      await Promise.allSettled(
        [...bodyPromises]
      );

      structured = bestStructuredEvidence(
        jsonBodies,
        itemId
      );
      decision = finalDecision(
        structured,
        dom
      );

      attempts.push({
        name: "purchase-area-retry",
        decision,
        dom: {
          enabledAdd: dom.enabledAdd,
          disabledAdd: dom.disabledAdd,
          buyNowEnabled: dom.buyNowEnabled,
          availabilityMeta: dom.availabilityMeta,
          exactStockTexts: dom.exactStockTexts,
          fulfillment: dom.fulfillment,
        },
      });
    }

    if (decision.stock === STOCK.UNKNOWN) {
      await client.send(
        "Page.reload",
        {
          ignoreCache: false,
        },
        sessionId,
        config.navigation_timeout_ms
      );

      await preserveMinimizedWindow(
        client,
        windowProtection
      );

      await waitForPage(
        client,
        sessionId,
        {},
        config.navigation_timeout_ms,
        config.settle_timeout_ms
      );

      await preserveMinimizedWindow(
        client,
        windowProtection
      );

      await triggerPurchaseAreaLoad(
        client,
        sessionId
      );

      await sleep(2500);

      dom = await collectDomPass(
        client,
        sessionId,
        itemId,
        jsonBodies
      );

      await Promise.allSettled(
        [...bodyPromises]
      );

      structured = bestStructuredEvidence(
        jsonBodies,
        itemId
      );
      decision = finalDecision(
        structured,
        dom
      );

      attempts.push({
        name: "reload-retry",
        decision,
        dom: {
          enabledAdd: dom.enabledAdd,
          disabledAdd: dom.disabledAdd,
          buyNowEnabled: dom.buyNowEnabled,
          availabilityMeta: dom.availabilityMeta,
          exactStockTexts: dom.exactStockTexts,
          fulfillment: dom.fulfillment,
        },
      });
    }

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
        scraperTabMode:
          scraperTarget?.mode || "unknown",
        scraperTabKeptOpen: true,
        focusProtection:
          "adopt_existing_or_create_focus_false",
        windowWasMinimized:
          Boolean(windowProtection?.wasMinimized),
        requestedItemId: itemId,
        pageUrl: dom.pageUrl,
        structuredBest: structured.best,
        structuredCandidates: structured.candidates,
        structuredConsensus:
          decision.consensus || null,
        attempts,
        dom: {
          title: dom.title,
          priceText: dom.priceText,
          availabilityMeta: dom.availabilityMeta,
          enabledAdd: dom.enabledAdd,
          disabledAdd: dom.disabledAdd,
          buyNowEnabled: dom.buyNowEnabled,
          cartControlLabel: dom.cartControlLabel,
          cartControlToken: dom.cartControlToken,
          offerControls: dom.offerControls,
          exactStockTexts: dom.exactStockTexts,
          purchaseTexts: dom.purchaseTexts,
          fulfillment: dom.fulfillment,
          selectedVariantSignals: dom.selectedVariantSignals,
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
    // Keep the dedicated Walmart tab open. Closing it can terminate the
    // entire Brave debug window when it is the browser's only usable tab.
    // The next Walmart item reuses this same target instead of creating
    // another tab.
    if (sessionId) {
      await client.send(
        "Target.detachFromTarget",
        {
          sessionId,
        },
        "",
        5000
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
      stockFromText("https://schema.org/InStock"),
      STOCK.IN_STOCK,
    ],
    [
      stockFromText("https://schema.org/OutOfStock"),
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

  if (
    !scraperTabStatePath("C:/test")
      .replace(/\\/g, "/")
      .endsWith("/logs/walmart_runtime_state.json")
  ) {
    throw new Error(
      "Self-test failed: invalid scraper tab state path."
    );
  }

  process.stdout.write(
    "Walmart variant and marketplace offer runtime self-test passed.\n"
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
