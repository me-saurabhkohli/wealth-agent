"use strict";

/* Wealth Agent demo viewer.
 *
 * Plays back recorded runs of the Python pipeline (docs/data.json, also
 * embedded as docs/data.js so the page works opened as a plain file).
 * No computation happens here: the trades, the compliance verdicts, and
 * the plan text were all produced by the Python demo on synthetic data.
 */

const state = {
  data: null,
  client: null,
  step: 0,
  playing: false,
  timer: null,
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function money(value) {
  return "$" + Number(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function num(value, digits) {
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function holdingPeriodTerm(acquired, asOf) {
  const days = Math.round((Date.parse(asOf) - Date.parse(acquired)) / 86400000);
  return days > 365 ? "long" : "short";
}

/* ---------------- portfolio ---------------- */

function renderPortfolio(client) {
  const container = document.getElementById("portfolio");
  container.textContent = "";
  const market = client.market_data;
  let total = 0;

  for (const holding of client.holdings) {
    const price = market[holding.ticker] ? market[holding.ticker].price : 0;
    const value = holding.shares * price;
    total += value;

    const table = el("table");
    const head = el("tr");
    for (const label of [holding.ticker, holding.asset_class, "", ""]) {
      head.appendChild(el("th", "", label));
    }
    table.appendChild(head);
    const row = el("tr");
    row.appendChild(el("td", "", num(holding.shares, 2) + " shares"));
    row.appendChild(el("td", "", "price " + money(price)));
    row.appendChild(el("td", "", "value " + money(value)));
    row.appendChild(el("td", "", ""));
    table.appendChild(row);
    container.appendChild(table);

    const lots = el("table", "lots");
    const lhead = el("tr");
    for (const label of ["Lot shares", "Cost basis", "Acquired", "Term", "Gain per share"]) {
      lhead.appendChild(el("th", "", label));
    }
    lots.appendChild(lhead);
    for (const lot of holding.lots) {
      const term = holdingPeriodTerm(lot.acquired, client.as_of);
      const tr = el("tr");
      tr.appendChild(el("td", "", num(lot.shares, 2)));
      tr.appendChild(el("td", "", money(lot.cost_basis)));
      tr.appendChild(el("td", "", lot.acquired));
      const termCell = el("td", term === "long" ? "term-long" : "term-short", term);
      tr.appendChild(termCell);
      const gain = price - lot.cost_basis;
      tr.appendChild(el("td", "", (gain >= 0 ? "+" : "") + money(gain)));
      lots.appendChild(tr);
    }
    container.appendChild(lots);
  }

  const totalLine = el("p", "", "Portfolio value at mock prices: " + money(total) +
    "  |  Target: " + Math.round(client.targets.stock * 100) + "/" +
    Math.round(client.targets.bond * 100) + " stock/bond");
  container.appendChild(totalLine);

  const salesBox = document.getElementById("recent-sales");
  salesBox.textContent = "";
  if (client.recent_sales && client.recent_sales.length) {
    salesBox.appendChild(el("h3", "", "Recent sales"));
    for (const sale of client.recent_sales) {
      salesBox.appendChild(el("p", "skipped",
        sale.ticker + " sold on " + sale.date + ", realized " + money(sale.realized_gain_loss) +
        " (inside the wash sale window if a buy follows within 30 days)"));
    }
  }
}

/* ---------------- market data ---------------- */

function renderMarketData(client) {
  const container = document.getElementById("market-data");
  container.textContent = "";
  const chips = el("div", "chips");
  for (const ticker of Object.keys(client.market_data).sort()) {
    const row = client.market_data[ticker];
    const chip = el("span", "chip");
    chip.appendChild(el("b", "", ticker));
    chip.appendChild(document.createTextNode(
      " " + money(row.price) + " | vol " + num(row.volatility, 2) + " | sent " + num(row.sentiment, 2)));
    chips.appendChild(chip);
  }
  container.appendChild(chips);
}

/* ---------------- trades ---------------- */

function renderTrades(client) {
  const container = document.getElementById("trades");
  container.textContent = "";
  const tax = client.tax_result;
  const trades = tax.recommended_trades || [];
  if (!trades.length) {
    container.appendChild(el("p", "empty", "No trades proposed."));
  } else {
    const table = el("table");
    const head = el("tr");
    for (const label of ["Side", "Ticker", "Shares", "Price", "Proceeds", "Realized", "Lot basis", "Term"]) {
      head.appendChild(el("th", label === "Shares" || label === "Price" || label === "Proceeds" || label === "Realized" || label === "Lot basis" ? "num" : "", label));
    }
    table.appendChild(head);
    for (const trade of trades) {
      const tr = el("tr");
      tr.appendChild(el("td", trade.side === "buy" ? "side-buy" : "side-sell", trade.side));
      tr.appendChild(el("td", "", trade.ticker));
      tr.appendChild(el("td", "num", num(trade.shares, 2)));
      tr.appendChild(el("td", "num", money(trade.price)));
      tr.appendChild(el("td", "num", money(trade.proceeds)));
      tr.appendChild(el("td", "num", trade.side === "sell" ? money(trade.realized_gain_loss) : ""));
      tr.appendChild(el("td", "num", trade.lot_cost_basis === null ? "" : money(trade.lot_cost_basis)));
      tr.appendChild(el("td", "", trade.term || ""));
      table.appendChild(tr);
    }
    container.appendChild(table);
  }

  const summary = el("p", "",
    "Net realized: " + money(tax.net_realized_gain_loss) +
    " (short term " + money(tax.gain_loss_breakdown.short_term_net) +
    ", long term " + money(tax.gain_loss_breakdown.long_term_net) + ")" +
    "  |  Estimated tax saving: " + money(tax.tax_savings_estimate) +
    "  |  Drift " + num(tax.drift_before_pct, 2) + " to " + num(tax.drift_after_pct, 2) + " points");
  container.appendChild(summary);

  const skippedBox = document.getElementById("skipped-lots");
  skippedBox.textContent = "";
  if (tax.skipped_lots && tax.skipped_lots.length) {
    skippedBox.appendChild(el("h3", "", "Loss lots the optimizer skipped"));
    const list = el("ul", "skipped");
    for (const lot of tax.skipped_lots) {
      list.appendChild(el("li", "",
        lot.ticker + ", " + num(lot.shares, 2) + " shares at " + money(lot.lot_cost_basis) +
        " basis (acquired " + lot.acquired + "): " + lot.reason));
    }
    skippedBox.appendChild(list);
  }
}

/* ---------------- compliance ---------------- */

function renderCompliance(client) {
  const container = document.getElementById("compliance");
  container.textContent = "";
  const list = el("ul", "check-list");
  for (const check of client.compliance.checks) {
    const item = el("li");
    item.appendChild(el("span", "status " + check.status, check.status));
    item.appendChild(el("span", "", check.rule.replace(/_/g, " ")));
    item.appendChild(el("span", "check-detail", check.detail));
    list.appendChild(item);
  }
  container.appendChild(list);
}

/* ---------------- outcome ---------------- */

function renderOutcome(client) {
  const container = document.getElementById("outcome");
  container.textContent = "";
  if (client.status === "complete") {
    const box = el("div", "complete-box");
    box.appendChild(el("p", "plan", client.plan));
    container.appendChild(box);
  } else {
    const box = el("div", "blocked-box");
    box.appendChild(el("p", "plan", "Blocked at the compliance gate. No plan was produced."));
    const list = el("ul", "skipped");
    for (const reason of client.block_reasons) {
      list.appendChild(el("li", "", reason));
    }
    box.appendChild(list);
    container.appendChild(box);
  }
}

/* ---------------- trace player ---------------- */

function renderTrace() {
  const client = state.client;
  const list = document.getElementById("trace-list");
  list.textContent = "";
  client.trace.forEach(function (event, index) {
    const item = el("li", "", "");
    if (index < state.step) item.classList.add("done");
    if (index === state.step) item.classList.add("current");
    const tok = event.tokens ? " [" + event.tokens + " tokens]" : "";
    item.textContent = event.seq + ". " + event.kind + " " + event.name + tok + "  " + event.detail;
    item.addEventListener("click", function () {
      stopPlay();
      state.step = index;
      renderTrace();
    });
    list.appendChild(item);
  });

  const detail = document.getElementById("step-detail");
  detail.textContent = "";
  const current = client.trace[state.step];
  if (current) {
    detail.appendChild(el("div", "kind", "Step " + current.seq + ": " + current.kind + " / " + current.name));
    detail.appendChild(el("div", "", current.detail + (current.tokens ? " (" + current.tokens + " tokens)" : "")));
  }
  document.getElementById("step-indicator").textContent =
    "Step " + (state.step + 1) + " of " + client.trace.length;
  document.getElementById("btn-prev").disabled = state.step === 0;
  document.getElementById("btn-next").disabled = state.step >= client.trace.length - 1;

  const byStep = Object.entries(client.tokens_by_step)
    .map(function (pair) { return pair[0] + ": " + pair[1]; })
    .join(", ");
  document.getElementById("token-summary").textContent =
    "Total tokens: " + client.tokens_used + " (by step: " + byStep + "). " +
    "Tax and compliance steps record 0: there is no model call in them to pay for.";
}

function stopPlay() {
  state.playing = false;
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  document.getElementById("btn-play").textContent = "Play";
}

function togglePlay() {
  if (state.playing) {
    stopPlay();
    return;
  }
  if (state.step >= state.client.trace.length - 1) state.step = 0;
  state.playing = true;
  document.getElementById("btn-play").textContent = "Pause";
  state.timer = setInterval(function () {
    if (state.step >= state.client.trace.length - 1) {
      stopPlay();
      return;
    }
    state.step += 1;
    renderTrace();
  }, 1100);
}

/* ---------------- client selection ---------------- */

function selectClient(clientId) {
  stopPlay();
  state.client = state.data.clients.find(function (c) { return c.client_id === clientId; });
  state.step = 0;
  document.querySelectorAll(".client-btn").forEach(function (btn) {
    btn.classList.toggle("active", btn.dataset.clientId === clientId);
  });
  document.getElementById("client-goal").textContent =
    state.client.goal + "  |  As of " + state.client.as_of;
  renderPortfolio(state.client);
  renderMarketData(state.client);
  renderTrades(state.client);
  renderCompliance(state.client);
  renderOutcome(state.client);
  renderTrace();
}

function init(data) {
  state.data = data;
  const picker = document.getElementById("client-picker");
  picker.textContent = "";
  for (const client of data.clients) {
    const btn = el("button", "client-btn", "");
    btn.type = "button";
    btn.dataset.clientId = client.client_id;
    btn.appendChild(document.createTextNode(client.client_id + " "));
    btn.appendChild(el("span", "badge", client.status === "complete" ? "(plan)" : "(blocked)"));
    btn.addEventListener("click", function () { selectClient(client.client_id); });
    picker.appendChild(btn);
  }
  document.getElementById("btn-prev").addEventListener("click", function () {
    stopPlay();
    if (state.step > 0) { state.step -= 1; renderTrace(); }
  });
  document.getElementById("btn-next").addEventListener("click", function () {
    stopPlay();
    if (state.step < state.client.trace.length - 1) { state.step += 1; renderTrace(); }
  });
  document.getElementById("btn-play").addEventListener("click", togglePlay);
  selectClient(data.clients[0].client_id);
}

if (window.DEMO_DATA) {
  init(window.DEMO_DATA);
} else {
  fetch("data.json")
    .then(function (response) { return response.json(); })
    .then(init)
    .catch(function () {
      document.getElementById("client-goal").textContent =
        "Could not load data.json. Run tools/build_demo_data.py to generate it.";
    });
}
