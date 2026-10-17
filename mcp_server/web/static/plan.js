// The Vanna plan / signing page. The server holds the plan; this page only shows it, asks the user's wallet to approve
// and sign, and relays what the wallet returns. It never composes a transaction or an amount.
//
// One main button, whose job follows the plan's state: "Approve and sign" (approve, then sign each step), "Continue"
// (an approved plan with steps left), "Try again" (the last step could not be prepared). Nothing else competes with it.
(function () {
  "use strict";
  var planId = decodeURIComponent(location.pathname.split("/").pop());
  var api = "/api/plan/" + encodeURIComponent(planId);
  var $ = function (id) { return document.getElementById(id); };
  var running = false, view = null, lastFailed = false;
  // The run token the approval returned: only the page that approved (or re-confirmed with the wallet) can act on the plan.
  var tokenKey = "vanna-plan-token:" + planId, runToken = null;
  try { runToken = sessionStorage.getItem(tokenKey); } catch (e) { /* storage blocked: re-confirm with the wallet */ }
  function keepToken(t) { runToken = t || null; try { if (t) sessionStorage.setItem(tokenKey, t); } catch (e) { /* kept in memory */ } }

  var FINAL = ["completed", "failed", "cancelled", "expired"];
  var STATUS_TEXT = { awaiting_approval: "Waiting for approval", approved: "Approved", running: "In progress", paused: "Paused",
    completed: "Done", failed: "Stopped", cancelled: "Cancelled", expired: "Expired" };
  var STEP_TEXT = { built: "Ready to sign", submitting: "Sending", submitted: "Confirming", confirmed: "Done", failed: "Failed",
    uncertain: "Outcome unclear", skipped: "Skipped", expired: "Did not land" };

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function short(s) { return s && s.length > 16 ? s.slice(0, 6) + "…" + s.slice(-6) : s; }

  async function call(method, path, body) {
    var headers = body ? { "Content-Type": "application/json" } : {};
    if (runToken) headers["X-Plan-Token"] = runToken;
    var r = await fetch(api + path, { method: method, headers: headers, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
    var data = await r.json().catch(function () { return {}; });
    if (!r.ok) { var e = new Error(data.message || ("Request failed (" + r.status + ")")); e.code = data.error; throw e; }
    return data;
  }

  function say(msg, kind) { var n = $("msg"); if (!msg) { n.hidden = true; return; } n.hidden = false; n.className = "notice" + (kind ? " " + kind : ""); n.textContent = msg; }

  function pill(status) {
    var cls = { completed: "ok", awaiting_approval: "warn", paused: "warn", failed: "bad", expired: "bad", cancelled: "bad" }[status] || "";
    return el("span", "pill " + cls, STATUS_TEXT[status] || status.replace(/_/g, " "));
  }

  // Approval window: counts down locally from the server's figure; the server still decides (it refuses late approvals).
  var deadline = 0;
  function tick() {
    var c = $("countdown");
    if (!view || view.status !== "awaiting_approval" || !deadline) { c.textContent = ""; return; }
    var left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    c.textContent = left > 0 ? Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0") + " left to approve" : "Approval window closed";
    c.className = "small " + (left > 0 && left < 60 ? "late" : "muted");
  }

  function fmtHf(x) { return x == null ? "No debt" : Number(x).toFixed(2); }

  function renderButtons() {
    var v = view, p = $("primary"), c = $("cancel");
    var open = v && FINAL.indexOf(v.status) < 0;
    var label = null;
    if (v && v.status === "awaiting_approval") label = "Approve and sign";
    else if (open) label = lastFailed ? "Try again" : "Continue";
    p.hidden = !label || running;
    if (label) p.textContent = label;
    c.hidden = !open || running;
  }

  function render(v) {
    view = v;
    $("loading").hidden = true; $("plan").hidden = false;
    document.title = "Vanna · " + v.summary;
    $("summary").textContent = v.summary;
    var st = $("status"); clear(st); st.appendChild(pill(v.status));
    deadline = v.status === "awaiting_approval" ? Date.now() + (v.approve_within_seconds || 0) * 1000 : 0; tick();
    $("wallet").textContent = v.wallet;

    var hf = $("hf"), s = v.simulation;
    hf.hidden = !s;
    if (s) { $("hf-now").textContent = fmtHf(s.hf_before); $("hf-after").textContent = fmtHf(s.hf_after); }

    var w = $("warnings"); clear(w);
    (v.warnings || []).forEach(function (t) { w.appendChild(el("li", null, t)); });
    $("details").hidden = !(v.warnings && v.warnings.length);

    var steps = $("steps"); clear(steps);
    v.steps.forEach(function (st) {
      var li = el("li", st.state);
      li.appendChild(el("div", "dot " + st.state, st.state === "confirmed" ? "✓" : String(st.index + 1)));
      var body = el("div");
      body.appendChild(el("div", null, st.label));
      var meta = [];
      if (STEP_TEXT[st.state]) meta.push(STEP_TEXT[st.state]);
      if (st.signature) meta.push("tx " + short(st.signature));
      if (meta.length) body.appendChild(el("div", "muted small", meta.join(" · ")));
      // A step's own error only once it has actually failed; a step that merely could not be prepared yet is reported
      // once, in the notice under the plan, with the way forward.
      if (st.error && (st.state === "failed" || st.state === "uncertain")) body.appendChild(el("div", "small bad-text", st.error));
      li.appendChild(body); steps.appendChild(li);
    });

    if (v.status === "completed") say("All done. Every step is confirmed on-chain.", "ok");
    else if (v.status === "failed") say("The plan stopped: a step failed or its outcome is unclear, so nothing further will run.", "bad");
    else if (v.status === "expired") say("This plan expired before it was approved. Ask your assistant to draw it again.", "bad");
    else if (v.status === "cancelled") say("This plan was cancelled. Steps already done stay done.", "bad");
    renderButtons();
  }

  async function refresh() { render(await call("GET", "")); }

  async function connectWallet() {
    var w = await VannaWallet.connect();
    if (view && w.address !== view.wallet) {
      VannaWallet.reset();
      throw new Error("This plan is for " + short(view.wallet) + ", but your wallet is on " + short(w.address) + ". Switch accounts in your wallet and try again.");
    }
    return w;
  }

  async function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  async function waitConfirmed(stepIndex) {
    for (var i = 0; i < 90; i++) {
      var v = await call("GET", ""); render(v);
      var s = v.steps[stepIndex].state;
      if (s === "confirmed" || s === "failed" || s === "expired" || s === "uncertain") return s;
      await sleep(2000);
    }
    return "submitted";
  }

  // An approved plan opened in another browser (or after the token was lost): the wallet re-confirms it here.
  async function ensureToken(w) {
    if (runToken || !view || !view.resume_message) return;
    var sig = await VannaWallet.signMessage(view.resume_message);
    var r = await call("POST", "/approve", { wallet: w.address, signature: sig });
    keepToken(r.run_token); render(r);
  }

  function showCurrent(r, total) {
    var box = $("current"); clear(box); box.hidden = false;
    box.appendChild(el("div", "muted small", "Step " + (r.step + 1) + " of " + total));
    box.appendChild(el("h2", null, r.label));
    if (r.risk && r.risk.reasons && r.risk.reasons.length) r.risk.reasons.forEach(function (t) { box.appendChild(el("div", "notice warn", t)); });
    box.appendChild(el("div", "muted", "Check it in your wallet and approve there."));
  }

  async function runSteps() {
    await ensureToken(await connectWallet());
    for (;;) {
      var r = await call("POST", "/next");
      if (r.plan) render(r.plan);
      if (r.done) return;
      if (r.skipped) continue;
      if (r.waiting) { await sleep(2000); continue; }
      if (r.ok === false) { lastFailed = true; say((r.reason || "This step could not be prepared.") + " Nothing was sent. Press Try again.", "bad"); return; }
      if (!r.transactions) return;
      showCurrent(r, view.steps.length);
      var idx = r.step;
      if (r.batch) {
        // One approval for the whole trade; the server sends them in order, each confirmed before the next.
        var all = await VannaWallet.signAllTransactions(r.transactions.map(function (t) { return t.unsignedTransaction; }));
        for (var j = 0; j < r.transactions.length; j++) {
          var sent = await call("POST", "/submit", { tx_id: r.transactions[j].id, signed: all[j] });
          if (sent.plan) render(sent.plan);
        }
      } else {
        for (var i = 0; i < r.transactions.length; i++) {
          var signed = await VannaWallet.signTransaction(r.transactions[i].unsignedTransaction);
          var sub = await call("POST", "/submit", { tx_id: r.transactions[i].id, signed: signed });
          if (sub.plan) render(sub.plan);
        }
      }
      $("current").hidden = true;
      var outcome = await waitConfirmed(idx);
      if (outcome !== "confirmed") {
        lastFailed = true;
        say(outcome === "expired" ? "The transaction did not land in time. Press Try again to rebuild it." : "The step did not confirm.", "bad");
        return;
      }
    }
  }

  async function approveAndRun() {
    var w = await connectWallet();
    var sig = await VannaWallet.signMessage(view.approval_message);
    var r = await call("POST", "/approve", { wallet: w.address, signature: sig });
    keepToken(r.run_token); render(r);
    await runSteps();
  }

  async function primary() {
    if (running || !view) return;
    running = true; lastFailed = false; say(""); renderButtons();
    try {
      if (view.status === "awaiting_approval") await approveAndRun(); else await runSteps();
    } catch (e) { lastFailed = view && view.status !== "awaiting_approval"; say(e.message, "bad"); }
    running = false; $("current").hidden = true;
    try { await refresh(); } catch (e) { renderButtons(); }
  }

  async function cancel() {
    if (!window.confirm("Cancel this plan? Steps already confirmed stay done; nothing else will run.")) return;
    try { lastFailed = false; render(await call("POST", "/cancel", {})); } catch (e) { say(e.message, "bad"); }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("primary").onclick = primary; $("cancel").onclick = cancel;
    refresh().catch(function (e) { $("loading").hidden = true; say(e.message, "bad"); });
    setInterval(tick, 1000);
    setInterval(function () { if (!running && view && FINAL.indexOf(view.status) < 0) refresh().catch(function () {}); }, 5000);
  });
})();
