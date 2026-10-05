// Injected-wallet access for the Vanna pages (Phantom, Backpack, Solflare). No keys are ever handled here:
// the wallet signs, this file only asks it to. All text shown to the user is set with textContent.
(function () {
  "use strict";

  function providers() {
    var found = [];
    var w = window;
    if (w.phantom && w.phantom.solana && w.phantom.solana.isPhantom) found.push({ name: "Phantom", p: w.phantom.solana });
    if (w.backpack && (w.backpack.isBackpack || w.backpack.connect)) found.push({ name: "Backpack", p: w.backpack });
    if (w.solflare && w.solflare.isSolflare) found.push({ name: "Solflare", p: w.solflare });
    if (!found.length && w.solana && w.solana.connect) found.push({ name: "Wallet", p: w.solana });
    return found;
  }

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

  var REJECTED = /user rejected|rejected the request|user denied|denied by the user|user cancel|declined|4001/i;
  function isRejection(e) { return REJECTED.test((e && (e.name + " " + e.message)) || String(e)); }

  var current = null;

  async function connect(preferredName) {
    var list = providers();
    if (!list.length) throw new Error("No Solana wallet was found in this browser. Install Phantom, Backpack or Solflare and reload.");
    var chosen = list.find(function (x) { return x.name === preferredName; }) || list[0];
    var res = await chosen.p.connect();
    var key = (res && res.publicKey) || chosen.p.publicKey;
    if (!key) throw new Error("The wallet did not return an address.");
    current = { name: chosen.name, p: chosen.p, address: key.toString() };
    return current;
  }

  async function signMessage(text) {
    if (!current) throw new Error("Connect a wallet first.");
    var bytes = new TextEncoder().encode(text);
    var res;
    try { res = await current.p.signMessage(bytes, "utf8"); } catch (e) { if (isRejection(e)) throw new Error("You declined the signature in your wallet. Nothing was approved."); throw e; }
    var sig = res && res.signature ? res.signature : res;
    return b64(new Uint8Array(sig));
  }

  async function signTransaction(unsignedB64) {
    if (!current) throw new Error("Connect a wallet first.");
    var tx = solanaWeb3.VersionedTransaction.deserialize(fromB64(unsignedB64));
    var signed;
    try { signed = await current.p.signTransaction(tx); } catch (e) { if (isRejection(e)) throw new Error("You declined the transaction in your wallet. Nothing was sent."); throw e; }
    return b64(signed.serialize());
  }

  window.VannaWallet = { providers: providers, connect: connect, signMessage: signMessage, signTransaction: signTransaction, current: function () { return current; } };
})();
