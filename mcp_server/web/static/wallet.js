// Wallet access for the Vanna pages. No keys are ever handled here: the wallet signs, this file only asks it to.
// Wallets are found through the Wallet Standard (any compliant Solana wallet: Phantom, Backpack, Solflare, ...), with
// the older injected objects (window.phantom.solana, ...) as a fallback. All text shown to the user is set with
// textContent.
(function () {
  "use strict";

  // ------------------------------------------------------------------ discovery

  var standard = [];
  function addStandard() {
    for (var i = 0; i < arguments.length; i++) {
      var w = arguments[i];
      var solana = w && w.chains && w.chains.some(function (c) { return String(c).indexOf("solana:") === 0; });
      var f = (w && w.features) || {};
      if (solana && f["standard:connect"] && f["solana:signTransaction"] && f["solana:signMessage"] && standard.indexOf(w) < 0) standard.push(w);
    }
    return function () {};
  }
  var api = Object.freeze({ register: addStandard });
  try {
    window.addEventListener("wallet-standard:register-wallet", function (e) { try { e.detail(api); } catch (x) { /* a broken wallet is skipped */ } });
    window.dispatchEvent(new CustomEvent("wallet-standard:app-ready", { detail: api }));
  } catch (e) { /* very old browser: the injected fallback still works */ }

  function legacy() {
    var found = [], w = window;
    if (w.phantom && w.phantom.solana && w.phantom.solana.isPhantom) found.push({ name: "Phantom", p: w.phantom.solana });
    if (w.backpack && (w.backpack.isBackpack || w.backpack.connect)) found.push({ name: "Backpack", p: w.backpack });
    if (w.solflare && w.solflare.isSolflare) found.push({ name: "Solflare", p: w.solflare });
    if (!found.length && w.solana && w.solana.connect) found.push({ name: "Wallet", p: w.solana });
    return found;
  }

  // One entry per wallet name: the Wallet Standard version when a wallet offers both.
  function providers() {
    var out = [], seen = {};
    standard.forEach(function (w) { if (!seen[w.name]) { seen[w.name] = 1; out.push({ name: w.name, icon: w.icon, std: w }); } });
    legacy().forEach(function (x) { if (!seen[x.name]) { seen[x.name] = 1; out.push({ name: x.name, p: x.p }); } });
    return out;
  }

  function isMobile() { return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || ""); }

  // Open this same page inside a wallet's own browser (phones without an extension).
  function mobileLinks() {
    var url = encodeURIComponent(location.href), ref = encodeURIComponent(location.origin);
    return [
      { name: "Open in Phantom", href: "https://phantom.app/ul/browse/" + url + "?ref=" + ref },
      { name: "Open in Solflare", href: "https://solflare.com/ul/v1/browse/" + url + "?ref=" + ref },
      { name: "Open in Backpack", href: "https://backpack.app/ul/v1/browse/" + url + "?ref=" + ref }
    ];
  }

  // ------------------------------------------------------------------ helpers

  function b64(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function fromB64(text) {
    var s = atob(text), out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  var REJECTED = /user rejected|rejected the request|user denied|denied by the user|user cancel|declined|4001/i;
  function isRejection(e) { return REJECTED.test((e && (e.name + " " + e.message)) || String(e)); }
  function declined(e, what) { if (isRejection(e)) throw new Error(what); throw e; }

  // ------------------------------------------------------------------ picking a wallet

  var current = null, chosenName = null;

  // Resolves with a wallet name: at once when there is one wallet (or one was already chosen), else after the user
  // picks one in the page's #picker card. Rejects when there is none, after showing the phone links when relevant.
  function pick() {
    var list = providers();
    if (chosenName && list.some(function (x) { return x.name === chosenName; })) return Promise.resolve(chosenName);
    if (list.length === 1) return Promise.resolve(list[0].name);
    var card = document.getElementById("picker");
    if (!list.length) {
      if (card && isMobile()) {
        showPicker(card, [], true);
        return new Promise(function (_, reject) {
          document.getElementById("picker-cancel").onclick = function () { card.hidden = true; reject(new Error("Open this page in your wallet's browser to continue.")); };
        });
      }
      return Promise.reject(new Error("No Solana wallet was found in this browser. Install Phantom, Backpack or Solflare and reload, or open this page in your wallet app."));
    }
    if (!card) return Promise.resolve(list[0].name);
    return new Promise(function (resolve, reject) {
      showPicker(card, list, false, function (name) { card.hidden = true; resolve(name); });
      document.getElementById("picker-cancel").onclick = function () { card.hidden = true; reject(new Error("No wallet chosen. Nothing was signed.")); };
    });
  }

  function showPicker(card, list, mobile, onPick) {
    var box = document.getElementById("picker-list"), links = document.getElementById("picker-links");
    while (box.firstChild) box.removeChild(box.firstChild);
    while (links.firstChild) links.removeChild(links.firstChild);
    list.forEach(function (x) {
      var b = el("button", "wallet");
      if (x.icon && /^data:image\/(svg\+xml|png|webp|jpeg);base64,/.test(x.icon)) { var img = el("img"); img.src = x.icon; img.alt = ""; b.appendChild(img); }
      b.appendChild(el("span", null, x.name));
      b.onclick = function () { onPick(x.name); };
      box.appendChild(b);
    });
    document.getElementById("picker-mobile").hidden = !mobile;
    if (mobile) mobileLinks().forEach(function (l) { var a = el("a", "wallet", l.name); a.href = l.href; a.rel = "noopener noreferrer"; links.appendChild(a); });
    card.hidden = false;
  }

  // ------------------------------------------------------------------ the wallet calls

  async function connect() {
    var name = await pick();
    var entry = providers().find(function (x) { return x.name === name; });
    if (!entry) throw new Error("That wallet is no longer available. Reload the page.");
    var address, account = null;
    if (entry.std) {
      var res = await entry.std.features["standard:connect"].connect().catch(function (e) { declined(e, "You declined the connection in your wallet."); });
      var accounts = (res && res.accounts && res.accounts.length ? res.accounts : entry.std.accounts) || [];
      account = accounts.find(function (a) { return (a.chains || []).some(function (c) { return String(c).indexOf("solana:") === 0; }); }) || accounts[0];
      if (!account) throw new Error("The wallet did not return an address.");
      address = account.address;
    } else {
      var r = await entry.p.connect().catch(function (e) { declined(e, "You declined the connection in your wallet."); });
      var key = (r && r.publicKey) || entry.p.publicKey;
      if (!key) throw new Error("The wallet did not return an address.");
      address = key.toString();
    }
    chosenName = name;
    current = { name: name, address: address, std: entry.std || null, account: account, p: entry.p || null };
    return current;
  }

  async function signMessage(text) {
    if (!current) throw new Error("Connect a wallet first.");
    var bytes = new TextEncoder().encode(text), sig;
    try {
      if (current.std) {
        var out = await current.std.features["solana:signMessage"].signMessage({ account: current.account, message: bytes });
        sig = out[0].signature;
      } else {
        var res = await current.p.signMessage(bytes, "utf8");
        sig = res && res.signature ? res.signature : res;
      }
    } catch (e) { declined(e, "You declined the signature in your wallet. Nothing was approved."); }
    return b64(new Uint8Array(sig));
  }

  // Signs one or more serialized transactions in ONE wallet approval where the wallet allows it.
  async function signMany(unsignedList, what) {
    if (!current) throw new Error("Connect a wallet first.");
    try {
      if (current.std) {
        var inputs = unsignedList.map(function (t) { return { account: current.account, transaction: fromB64(t) }; });
        var outs = await current.std.features["solana:signTransaction"].signTransaction.apply(null, inputs);
        return outs.map(function (o) { return b64(new Uint8Array(o.signedTransaction)); });
      }
      var txs = unsignedList.map(function (t) { return solanaWeb3.VersionedTransaction.deserialize(fromB64(t)); });
      var signed;
      if (txs.length === 1) signed = [await current.p.signTransaction(txs[0])];
      else if (current.p.signAllTransactions) signed = await current.p.signAllTransactions(txs);
      else { signed = []; for (var i = 0; i < txs.length; i++) signed.push(await current.p.signTransaction(txs[i])); }
      return signed.map(function (t) { return b64(t.serialize()); });
    } catch (e) { declined(e, what); }
  }

  async function signTransaction(unsignedB64) {
    return (await signMany([unsignedB64], "You declined the transaction in your wallet. Nothing was sent."))[0];
  }

  // A Predict trade: every transaction signed in one approval, each keeping the app maker's signature already in it.
  async function signAllTransactions(unsignedList) {
    return signMany(unsignedList, "You declined the transactions in your wallet. Nothing was sent.");
  }

  // ------------------------------------------------------------------ the page frame (site check, network)

  function frame() {
    var app = document.getElementById("app");
    if (!app) return;
    var site = app.getAttribute("data-site") || "";
    var host = document.getElementById("site-host"); if (host) host.textContent = site;
    var net = document.getElementById("network");
    if (net && app.getAttribute("data-test") === "1") { net.textContent = "Test network"; net.title = app.getAttribute("data-network") || ""; net.hidden = false; }
    if (site && location.host !== site) {
      var bad = document.getElementById("site-bad");
      bad.textContent = "Warning: this page is on " + location.host + ", not " + site + ". Do not connect your wallet unless you are sure this is the Vanna signing page.";
      bad.hidden = false;
    }
  }
  document.addEventListener("DOMContentLoaded", frame);

  // Forget the chosen wallet (the user connected the wrong one and must be able to pick again).
  function reset() { current = null; chosenName = null; }

  window.VannaWallet = Object.freeze({ providers: providers, connect: connect, signMessage: signMessage, signTransaction: signTransaction,
    signAllTransactions: signAllTransactions, reset: reset, current: function () { return current; } });
})();
