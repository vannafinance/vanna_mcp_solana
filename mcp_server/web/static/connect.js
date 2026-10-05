// The Vanna wallet-connect page for MCP client login. The server decides everything (what is being authorized,
// the message the wallet signs, whether the signature verifies); this page shows it and relays one signature.
(function () {
  "use strict";
  var req = new URLSearchParams(location.search).get("req") || "";
  var $ = function (id) { return document.getElementById(id); };
  var info = null;

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function say(msg, bad) { var n = $("msg"); n.textContent = msg || ""; n.hidden = !msg; n.className = "notice" + (bad ? " bad" : ""); }

  async function post(path, body) {
    var r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store" });
    var data = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(data.message || ("Request failed (" + r.status + ")"));
    return data;
  }

  async function load() {
    if (!req) { say("This page needs a sign-in request from your app. Start the connection again from your MCP client.", true); return; }
    var r = await fetch("/oauth/consent/request?req=" + encodeURIComponent(req), { cache: "no-store" });
    var data = await r.json().catch(function () { return {}; });
    if (!r.ok) { say(data.message || "This sign-in request is not valid.", true); return; }
    info = data;
    $("client").textContent = data.client_name;
    $("host").textContent = data.redirect_host;
    var list = $("scopes"); while (list.firstChild) list.removeChild(list.firstChild);
    var wants = data.requested_scopes;
    var labels = { "vanna:read": "Read your balances, positions and market data", "vanna:write": "Prepare transactions for you to review and sign (it can never sign or send)" };
    wants.forEach(function (s) {
      var li = el("li"); var cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = true; cb.value = s; cb.id = "scope-" + s;
      if (s === "vanna:read") { cb.disabled = true; }
      var lab = el("label", null, " " + (labels[s] || s)); lab.htmlFor = cb.id; li.appendChild(cb); li.appendChild(lab); list.appendChild(li);
    });
    $("panel").hidden = false;
  }

  function chosenScopes() {
    var out = []; document.querySelectorAll("#scopes input").forEach(function (c) { if (c.checked) out.push(c.value); }); return out;
  }

  async function allow() {
    say(""); $("allow").disabled = true;
    try {
      var w = await VannaWallet.connect();
      var ch = await post("/oauth/consent/challenge", { req: req, wallet: w.address, scopes: chosenScopes() });
      var sig = await VannaWallet.signMessage(ch.message);
      var done = await post("/oauth/consent/complete", { req: req, wallet: w.address, nonce: ch.nonce, signature: sig });
      say("Connected. Returning to your app…", false);
      location.href = done.redirect_url;
    } catch (e) { say(e.message, true); $("allow").disabled = false; }
  }

  async function deny() {
    try { var done = await post("/oauth/consent/deny", { req: req }); location.href = done.redirect_url; } catch (e) { say(e.message, true); }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("allow").onclick = allow; $("deny").onclick = deny;
    load().catch(function (e) { say(e.message, true); });
  });
})();
