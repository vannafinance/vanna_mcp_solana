// The Vanna plan / signing page. The server holds the plan; this page only shows it, asks the user's wallet to approve
// and sign, and relays what the wallet returns. It never composes a transaction or an amount.
(function () {
  "use strict";
  var planId = decodeURIComponent(location.pathname.split("/").pop());
  var api = "/api/plan/" + encodeURIComponent(planId);
  var $ = function (id) { return document.getElementById(id); };
  var running = false, view = null;
  // The run token the approval returned: only the page that approved (or re-confirmed with the wallet) can act on the plan.
  var tokenKey = "vanna-plan-token:" + planId, runToken = null;
  try { runToken = sessionStorage.getItem(tokenKey); } catch (e) { /* storage blocked: re-confirm with the wallet */ }
  function keepToken(t) { runToken = t || null; try { if (t) sessionStorage.setItem(tokenKey, t); } catch (e) { /* kept in memory */ } }

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  async function call(method, path, body) {
    var headers = body ? { "Content-Type": "application/json" } : {};
    if (runToken) headers["X-Plan-Token"] = runToken;
    var r = await fetch(api + path, { method: method, headers: headers, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
    var data = await r.json().catch(function () { return {}; });
    if (!r.ok) { var e = new Error(data.message || ("Request failed (" + r.status + ")")); e.code = data.error; throw e; }
    return data;
  }

  function say(msg, bad) { var n = $("msg"); clear(n); if (!msg) { n.hidden = true; return; } n.hidden = false; n.className = "notice" + (bad ? " bad" : ""); n.textContent = msg; }

  function pill(status) {
    var cls = { completed: "ok", running: "", approved: "", awaiting_approval: "warn", paused: "warn", failed: "bad", expired: "bad", cancelled: "bad" }[status] || "";
    return el("span", "pill " + cls, status.replace(/_/g, " "));
  }

  function render(v) {
    view = v;
    $("summary").textContent = v.summary;
    var st = $("status"); clear(st); st.appendChild(pill(v.status));
    $("wallet").textContent = v.wallet;
    var w = $("warnings"); clear(w);
    (v.warnings || []).forEach(function (t) { w.appendChild(el("li", null, t)); });
    var sim = $("sim"); clear(sim);
    if (v.simulation) {
      var s = v.simulation, fmt = function (x) { return x == null ? "no debt" : Number(x).toFixed(2); };
      sim.appendChild(el("div", null, "Health factor: " + fmt(s.hf_before) + " now → " + fmt(s.hf_after) + " after (worst case). Floor " + Number(v.hf_floor).toFixed(2) + ", liquidation at 1.10."));
    }
    var steps = $("steps"); clear(steps);
    v.steps.forEach(function (s) {
      var li = el("li");
      li.appendChild(el("div", "dot " + s.state, s.state === "confirmed" ? "✓" : String(s.index + 1)));
      var body = el("div");
      body.appendChild(el("div", null, s.label));
      var meta = [];
      if (s.state !== "pending") meta.push(s.state);
      if (s.usd) meta.push("≈ $" + Number(s.usd).toFixed(2));
      if (meta.length) body.appendChild(el("div", "muted", meta.join(" · ")));
      (s.notes || []).forEach(function (n) { body.appendChild(el("div", "muted", n)); });
      if (s.error) body.appendChild(el("div", "muted", s.error));
      if (s.signature) body.appendChild(el("div", "mono muted", s.signature));
      li.appendChild(body); steps.appendChild(li);
    });
    var done = ["completed", "failed", "cancelled", "expired"].indexOf(v.status) >= 0;
    $("approve").hidden = v.status !== "awaiting_approval";
    $("run").hidden = !(v.status === "approved" || v.status === "running" || v.status === "paused");
    $("cancel").hidden = done;
    if (v.status === "completed") say("All steps are confirmed on-chain.", false);
    if (v.status === "failed") say("A step failed or has an unclear outcome, so the plan stopped. Nothing further will run.", true);
    if (v.status === "expired") say("This plan expired. Ask your assistant to draw it again.", true);
    if (v.status === "cancelled") say("This plan was cancelled.", true);
  }

  async function refresh() { render(await call("GET", "")); }

  async function connectWallet() {
    var w = await VannaWallet.connect();
    if (view && w.address !== view.wallet) {
      throw new Error("This plan was drawn for " + view.wallet + ". Switch your wallet to that address and try again (you are on " + w.address + ").");
    }
    return w;
  }

  async function approve() {
    say(""); $("approve").disabled = true;
    try {
      var w = await connectWallet();
      var sig = await VannaWallet.signMessage(view.approval_message);
      var r = await call("POST", "/approve", { wallet: w.address, signature: sig });
      keepToken(r.run_token); render(r);
      say("Approved. Review each step as it is prepared; nothing moves until you sign it.", false);
    } catch (e) { say(e.message, true); }
    $("approve").disabled = false;
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

  async function run() {
    if (running) return; running = true; say(""); $("run").disabled = true;
    try {
      await ensureToken(await connectWallet());
      for (;;) {
        var r = await call("POST", "/next");
        if (r.plan) render(r.plan);
        if (r.done) break;
        if (r.skipped) continue;
        if (r.waiting) { await sleep(2000); continue; }
        if (r.ok === false) { say(r.reason || "This step could not be prepared.", true); break; }
        if (!r.transactions) break;
        var box = $("current"); clear(box); box.hidden = false;
        box.appendChild(el("h2", null, r.label));
        if (r.risk && r.risk.reasons && r.risk.reasons.length) r.risk.reasons.forEach(function (t) { box.appendChild(el("div", "notice", t)); });
        (r.notes || []).forEach(function (t) { box.appendChild(el("div", "muted", t)); });
        box.appendChild(el("div", "muted", "Your wallet will show the transaction. Check it before you sign."));
        var ok = await new Promise(function (resolve) {
          var b = el("button", null, "Sign in wallet"); var c = el("button", "secondary", "Stop here");
          var row = el("div", "row"); row.appendChild(b); row.appendChild(c); box.appendChild(row);
          b.onclick = function () { b.disabled = true; c.disabled = true; resolve(true); };
          c.onclick = function () { resolve(false); };
        });
        if (!ok) { say("Stopped before signing. Nothing was sent for this step.", true); break; }
        var idx = r.step;
        if (r.batch) {
          // One approval for the whole trade; the server sends them in order, each confirmed before the next.
          var all = await VannaWallet.signAllTransactions(r.transactions.map(function (t) { return t.unsignedTransaction; }));
          for (var j = 0; j < r.transactions.length; j++) {
            if (r.transactions.length > 1) say("Sending " + (j + 1) + " of " + r.transactions.length + ": " + (r.transactions[j].purpose || ""));
            var sent = await call("POST", "/submit", { tx_id: r.transactions[j].id, signed: all[j] });
            if (sent.plan) render(sent.plan);
          }
          say("");
        } else {
          for (var i = 0; i < r.transactions.length; i++) {
            var t = r.transactions[i];
            var signed = await VannaWallet.signTransaction(t.unsignedTransaction);
            var sub = await call("POST", "/submit", { tx_id: t.id, signed: signed });
            if (sub.plan) render(sub.plan);
          }
        }
        clear(box); box.hidden = true;
        var outcome = await waitConfirmed(idx);
        if (outcome !== "confirmed") { say(outcome === "expired" ? "The transaction did not land in time; press Continue to rebuild it." : "The step did not confirm.", true); break; }
      }
    } catch (e) { say(e.message, true); }
    running = false; $("run").disabled = false;
    try { await refresh(); } catch (e) { /* the page keeps its last state */ }
  }

  async function cancel() { try { render(await call("POST", "/cancel", {})); } catch (e) { say(e.message, true); } }

  document.addEventListener("DOMContentLoaded", function () {
    $("approve").onclick = approve; $("run").onclick = run; $("cancel").onclick = cancel;
    refresh().catch(function (e) { say(e.message, true); });
    setInterval(function () { if (!running && view && ["completed", "failed", "cancelled", "expired"].indexOf(view.status) < 0) refresh().catch(function () {}); }, 5000);
  });
})();
