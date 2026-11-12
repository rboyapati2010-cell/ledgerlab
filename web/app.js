"use strict";
const $ = (id) => document.getElementById(id);
const money = (cents) =>
  cents == null
    ? "—"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
      }).format(cents / 100);
const state = { payments: [], entries: [], totals: {}, sample_csv: "" };
let selected = null,
  lastRequest = null,
  pendingRefund = null,
  busy = false;
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function note(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = "notice" + (error ? " error" : "");
  $("notice").hidden = false;
  $("notice").setAttribute("role", error ? "alert" : "status");
}
async function api(path, body, key) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const options = {
      signal: controller.signal,
      headers: { Accept: "application/json" },
    };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    if (key) options.headers["Idempotency-Key"] = key;
    const response = await fetch(path, options);
    const payload = await response.json();
    if (!response.ok) {
      const error = new Error(
        payload.message ||
          payload.error ||
          "The request could not be completed.",
      );
      error.responseConfirmed = response.status >= 400 && response.status < 500;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError")
      throw new Error(
        "The server took too long. The outcome is unconfirmed; retry the saved request if available.",
      );
    if (error instanceof TypeError)
      throw new Error(
        "Could not reach the local server. Check that LedgerLab is running, then retry.",
      );
    throw error;
  } finally {
    clearTimeout(timer);
  }
}
function controls() {
  document.querySelectorAll("main button").forEach((b) => (b.disabled = busy));
  $("repeat-button").disabled = busy || !lastRequest;
  $("retry-refund").hidden = !pendingRefund;
  $("retry-refund").disabled = busy || !pendingRefund;
  $("seed-button").disabled = busy || state.payments.length > 0;
  $("empty-seed").disabled = busy;
  const p = state.payments.find((p) => p.id === selected);
  $("refund-button").disabled = busy || !p || p.net_cents <= 0;
  document
    .querySelectorAll("form")
    .forEach((f) => f.setAttribute("aria-busy", String(busy)));
}
async function act(fn) {
  if (busy) return;
  busy = true;
  controls();
  try {
    await fn();
  } catch (error) {
    note(error.message, true);
  } finally {
    busy = false;
    controls();
  }
}
async function refresh() {
  Object.assign(state, await api("/api/state"));
  render();
}
function cents(text) {
  const value = text.trim();
  if (!/^\d+(\.\d{1,2})?$/.test(value))
    throw new Error(
      "Enter a positive dollar amount with no more than two decimal places, such as 49.00.",
    );
  const [whole, fraction = ""] = value.split(".");
  const result = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(result) || result <= 0)
    throw new Error(
      "The amount must be positive and within the supported range.",
    );
  return result;
}
function tag(text) {
  return node("span", text.replaceAll("_", " "), "tag " + text);
}
function render() {
  const focusedPaymentId = document.activeElement?.dataset.paymentId;
  const t = state.totals;
  $("gross").textContent = money(t.gross_cents || 0);
  $("refunded").textContent = money(t.refund_cents || 0);
  $("net").textContent = money(t.net_cents || 0);
  $("integrity").textContent =
    t.ledger_balance_cents === 0
      ? "Journal balanced · $0.00 difference"
      : "Ledger imbalance: " + money(t.ledger_balance_cents);
  $("payment-count").textContent = state.payments.length + " payments";
  const hasDemo = state.payments.some((p) => p.id.startsWith("pay_demo_"));
  $("seed-button").textContent = hasDemo
    ? "Synthetic samples loaded"
    : state.payments.length
      ? "Sample data needs an empty ledger"
      : "Load sample payments";
  $("seed-button").title = state.payments.length
    ? "Your existing ledger is kept intact."
    : "";
  $("payments-empty").hidden = state.payments.length > 0;
  $("payment-table-wrap").hidden = !state.payments.length;
  $("journal-empty").hidden = state.entries.length > 0;
  const body = $("payments-body");
  body.replaceChildren();
  for (const p of state.payments) {
    const tr = node("tr");
    if (p.id === selected) tr.className = "selected";
    const name = node("td");
    const button = node("button", p.description, "row-link");
    button.type = "button";
    button.dataset.paymentId = p.id;
    button.setAttribute("aria-label", "Inspect " + p.description);
    button.setAttribute("aria-pressed", String(p.id === selected));
    button.onclick = () => {
      selected = p.id;
      render();
    };
    name.append(button, node("span", p.id, "secondary-line"));
    tr.append(name);
    const status = node("td");
    status.append(tag(p.status));
    tr.append(
      status,
      node("td", money(p.amount_cents), "amount"),
      node("td", money(p.net_cents), "amount"),
    );
    body.append(tr);
  }
  const journal = $("journal-body");
  journal.replaceChildren();
  for (const e of state.entries) {
    const tr = node("tr");
    const identity = node("td", e.transaction_id, "code");
    identity.append(node("span", e.payment_id, "secondary-line"));
    const date = new Date(e.created_at);
    tr.append(
      identity,
      node("td", e.account.replaceAll("_", " ")),
      node("td", money(e.amount_cents), "amount"),
      node(
        "td",
        Number.isNaN(date.getTime()) ? e.created_at : date.toLocaleString(),
      ),
    );
    journal.append(tr);
  }
  const p = state.payments.find((p) => p.id === selected);
  $("selection-empty").hidden = !!p;
  $("refund-form").hidden = !p;
  if (p) {
    $("selected-description").textContent = p.description;
    $("selected-id").textContent = p.id;
    $("selected-balance").textContent =
      money(p.net_cents) +
      " available · " +
      money(p.refunded_cents) +
      " refunded";
    $("refund-hint").textContent =
      p.net_cents > 0
        ? "Refunds cannot exceed the remaining balance."
        : "This payment has been fully refunded.";
  }
  controls();
  // Selection rebuilds the table synchronously. Restore only the control
  // that held focus; form submissions and background refreshes keep theirs.
  if (focusedPaymentId && !busy) {
    const replacement = [...body.querySelectorAll("button")].find(
      (button) => button.dataset.paymentId === focusedPaymentId,
    );
    replacement?.focus({ preventScroll: true });
  }
}
const views = {
  payments: [
    "Payment ledger",
    "Record a payment. Follow the money. Keep both sides balanced.",
  ],
  journal: [
    "Journal entries",
    "Every operation leaves a balanced, inspectable accounting trail.",
  ],
  reconcile: [
    "Settlement reconciliation",
    "Compare external settlement rows with the ledger’s current balances.",
  ],
};
document.querySelectorAll("[data-view]").forEach((button) =>
  button.addEventListener("click", () => {
    const name = button.dataset.view;
    $("notice").hidden = true;
    for (const key of Object.keys(views))
      $("view-" + key).hidden = key !== name;
    document
      .querySelectorAll("[data-view]")
      .forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    $("view-title").textContent = views[name][0];
    $("view-subtitle").textContent = views[name][1];
  }),
);
async function seed() {
  await act(async () => {
    const result = await api("/api/demo/seed", {});
    await refresh();
    note(
      result.seeded
        ? "Loaded eight synthetic payments and two refunds. Select a payment to inspect it."
        : result.message || "Your existing ledger was preserved.",
    );
  });
}
$("seed-button").onclick = seed;
$("empty-seed").onclick = seed;
$("payment-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => {
    const body = {
      description: $("description").value.trim(),
      amount_cents: cents($("amount").value),
    };
    if (!body.description)
      throw new Error("Add a description for this payment.");
    lastRequest = { body, key: crypto.randomUUID() };
    const result = await api("/api/payments", body, lastRequest.key);
    selected = result.payment.id;
    await refresh();
    $("payment-form").reset();
    note(
      "Payment recorded. " +
        money(result.payment.amount_cents) +
        " added with balanced journal entries.",
    );
  });
});
$("repeat-button").onclick = () =>
  act(async () => {
    const result = await api(
      "/api/payments",
      lastRequest.body,
      lastRequest.key,
    );
    selected = result.payment.id;
    await refresh();
    note(
      result.replayed
        ? "Request replayed safely. The original payment was returned; no duplicate was created."
        : "The earlier request had not completed. Your payment is now recorded.",
    );
  });
// Keep the exact intent until the server confirms its outcome. A timeout may
// happen after the database commits, so retrying must reuse the original key.
async function completeRefund() {
  const request = pendingRefund;
  let result;
  try {
    result = await api(request.path, request.body, request.key);
  } catch (error) {
    if (error.responseConfirmed) pendingRefund = null;
    else
      error.message +=
        " Use “Retry pending refund” to check the same request safely.";
    throw error;
  }
  pendingRefund = null;
  selected = result.payment.id;
  $("refund-form").reset();
  await refresh();
  note(
    result.replayed
      ? "Refund request replayed safely. The original refund was returned; no duplicate was created."
      : "Refund recorded: " +
          money(request.body.amount_cents) +
          ". The journal remains balanced.",
  );
}
$("refund-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => {
    const amount = cents($("refund-amount").value),
      payment = state.payments.find((p) => p.id === selected);
    if (!payment) throw new Error("Select a payment first.");
    const path = "/api/payments/" + encodeURIComponent(payment.id) + "/refunds";
    if (pendingRefund) {
      if (
        pendingRefund.path !== path ||
        pendingRefund.body.amount_cents !== amount
      )
        throw new Error(
          "A previous refund has an unconfirmed outcome. Use “Retry pending refund” before starting a different refund.",
        );
    } else {
      if (amount > payment.net_cents)
        throw new Error(
          "Refund exceeds the available " +
            money(payment.net_cents) +
            ". Enter a smaller amount.",
        );
      pendingRefund = {
        path,
        body: { amount_cents: amount },
        key: crypto.randomUUID(),
      };
    }
    await completeRefund();
  });
});
$("retry-refund").onclick = () => act(completeRefund);
$("sample-csv").onclick = () => {
  $("settlement").value = state.sample_csv;
  if (!state.payments.length)
    note("Load sample payments first to see a useful reconciliation example.");
  else
    note(
      "Sample CSV loaded with intentional discrepancies. Select Reconcile to compare it.",
    );
};
$("reconcile-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act(async () => {
    const result = await api("/api/reconcile", { csv: $("settlement").value });
    $("reconcile-empty").hidden = true;
    $("reconcile-results").hidden = false;
    $("reconcile-summary").hidden = false;
    $("reconcile-count").textContent = result.rows.length + " results";
    const summary = $("reconcile-summary");
    summary.replaceChildren();
    for (const [label, count] of Object.entries(result.summary)) {
      const cell = node("span");
      cell.append(node("b", count + " "), document.createTextNode(label));
      summary.append(cell);
    }
    const body = $("reconcile-body");
    body.replaceChildren();
    for (const row of result.rows) {
      const tr = node("tr");
      const status = node("td");
      status.append(tag(row.status));
      tr.append(
        node("td", row.payment_id || "Invalid row", "code"),
        status,
        node("td", money(row.expected_cents), "amount"),
        node("td", money(row.actual_cents), "amount"),
        node("td", row.detail, "wrap-cell"),
      );
      body.append(tr);
    }
    note(
      "Reconciliation complete. Results reflect the ledger at the time of this comparison.",
    );
  });
});
act(refresh);
