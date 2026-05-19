/* dn42 auto-peer wizard — vanilla JS */
"use strict";
const dn42ctlPeer = (() => {
  const API = "/api/public/auto-peer";
  let _state = { step: 1, asn: 0, mntners: [], challenge: null, session: null };

  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return [...document.querySelectorAll(sel)]; }

  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }

  function themeToggle() {
    const isDark = document.documentElement.classList.toggle("dark");
    localStorage.setItem("theme", isDark ? "dark" : "light");
  }

  function setStep(n) {
    _state.step = n;
    $$("[data-step]").forEach(el => {
      const s = Number(el.dataset.step);
      el.classList.toggle("active", s <= n);
    });
  }

  async function post(path, body) {
    const opts = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    if (_state.session) opts.headers["Authorization"] = `Bearer ${_state.session}`;
    const res = await fetch(API + path, opts);
    const json = await res.json().catch(() => ({ detail: res.statusText }));
    if (!res.ok) throw new Error(json.detail || JSON.stringify(json));
    return json;
  }

  /* ── step 1: ASN lookup ─────────────────────────────────── */
  function renderStep1(err) {
    setStep(1);
    $("#wizard").innerHTML = `
      <section class="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
        <h2 class="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-4">Step 1 — Identify your AS</h2>
        <form id="step1-form" class="space-y-4">
          <label class="block">
            <span class="block text-xs uppercase tracking-wider text-zinc-500 mb-1">Your ASN</span>
            <input id="asn-input" type="number" min="1" required placeholder="e.g. 4242421234"
                   value="${_state.asn || ""}"
                   class="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100">
          </label>
          <button type="submit" class="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90">Look up</button>
          ${err ? `<p class="text-xs text-red-600 dark:text-red-400">${esc(err)}</p>` : ""}
        </form>
      </section>`;
    $("#step1-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const asn = Number($("#asn-input").value);
      try {
        const res = await post("/lookup", { asn });
        _state.asn = asn;
        _state.mntners = res.mntners;
        renderStep2();
      } catch (err) { renderStep1(err.message); }
    });
  }

  /* ── step 2: choose mntner + auth ───────────────────────── */
  function renderStep2() {
    setStep(2);
    const rows = _state.mntners.flatMap(m =>
      m.auth_options.map(opt => `
        <tr class="border-t border-zinc-100 dark:border-zinc-800 cursor-pointer hover:bg-zinc-100 dark:hover:bg-zinc-800"
            onclick="dn42ctlPeer.pickAuth('${esc(m.name)}', ${opt.index})">
          <td class="px-3 py-2 text-sm">${esc(m.name)}</td>
          <td class="px-3 py-2 text-sm font-mono">${esc(opt.scheme)}</td>
          <td class="px-3 py-2 text-sm">${opt.fingerprint ? esc(opt.fingerprint.slice(0,16)) + "..." : "—"}</td>
        </tr>`)
    ).join("");
    const empty = !rows ? `<p class="text-sm text-zinc-500 mt-2">No supported auth methods found.</p>` : "";
    $("#wizard").innerHTML = `
      <section class="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
        <h2 class="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-2">Step 2 — Choose authentication</h2>
        <p class="text-xs text-zinc-500 mb-4">AS${_state.asn} — click a row to proceed.</p>
        ${rows ? `<div class="overflow-x-auto"><table class="w-full">
          <thead><tr>
            <th class="text-left text-xs uppercase tracking-wider text-zinc-500 px-3 py-2">Maintainer</th>
            <th class="text-left text-xs uppercase tracking-wider text-zinc-500 px-3 py-2">Scheme</th>
            <th class="text-left text-xs uppercase tracking-wider text-zinc-500 px-3 py-2">Fingerprint</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table></div>` : empty}
        <button onclick="dn42ctlPeer.goStep1()" class="mt-4 text-xs underline text-zinc-500">Back</button>
      </section>`;
  }

  async function pickAuth(mntner, index) {
    try {
      const res = await post("/challenge", { asn: _state.asn, mntner, auth_index: index });
      _state.challenge = { ...res, mntner };
      renderStep3();
    } catch (err) { renderStep2(); alert(err.message); }
  }

  /* ── step 3: sign challenge ─────────────────────────────── */
  function renderStep3(err) {
    setStep(3);
    const c = _state.challenge;
    const isSSH = c.scheme === "ssh";
    const instructions = isSSH
      ? `<p class="text-sm mb-2">Run this on your machine:</p>
         <pre class="text-xs bg-white dark:bg-black border border-zinc-200 dark:border-zinc-800 rounded p-3 overflow-x-auto select-all">echo -n "${c.nonce}" > /tmp/dn42-challenge.txt
ssh-keygen -Y sign -n ${c.namespace} -f ~/.ssh/id_ed25519 /tmp/dn42-challenge.txt
cat /tmp/dn42-challenge.txt.sig</pre>
         <p class="text-xs text-zinc-500 mt-1">Use the key that matches your mntner's <code>auth:</code> line. Adjust <code>~/.ssh/id_ed25519</code> to your actual key path.</p>`
      : `<p class="text-sm mb-2">Run this on your machine:</p>
         <pre class="text-xs bg-white dark:bg-black border border-zinc-200 dark:border-zinc-800 rounded p-3 overflow-x-auto select-all">echo -n "${c.nonce}" | gpg --clearsign</pre>
         <p class="text-xs text-zinc-500 mt-1">Use the PGP key registered in your mntner.</p>`;

    $("#wizard").innerHTML = `
      <section class="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
        <h2 class="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-2">Step 3 — Sign the challenge</h2>
        <p class="text-xs text-zinc-500 mb-4">Maintainer: <strong>${esc(c.mntner)}</strong> / Scheme: <strong>${esc(c.scheme)}</strong></p>
        <div class="mb-4 rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-100 dark:bg-zinc-800 px-4 py-3">
          <p class="text-xs uppercase tracking-wider text-zinc-500 mb-1">Nonce (hex)</p>
          <code class="text-sm break-all select-all">${esc(c.nonce)}</code>
        </div>
        ${instructions}
        <form id="step3-form" class="mt-4 space-y-3">
          <label class="block">
            <span class="block text-xs uppercase tracking-wider text-zinc-500 mb-1">Paste your signature</span>
            <textarea id="sig-input" rows="8" required
                      class="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100"></textarea>
          </label>
          <div class="flex gap-2">
            <button type="submit" class="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90">Verify</button>
            <button type="button" onclick="dn42ctlPeer.goStep2()" class="rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm">Back</button>
          </div>
          ${err ? `<p class="text-xs text-red-600 dark:text-red-400">${esc(err)}</p>` : ""}
        </form>
      </section>`;
    $("#step3-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const res = await post("/verify", { challenge_id: c.challenge_id, signature: $("#sig-input").value });
        _state.session = res.peer_session_token;
        renderStep4();
      } catch (err) { renderStep3(err.message); }
    });
  }

  /* ── step 4: submit peer info ───────────────────────────── */
  function renderStep4(err) {
    setStep(4);
    $("#wizard").innerHTML = `
      <section class="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
        <h2 class="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-2">Step 4 — Submit your peering info</h2>
        <p class="text-xs text-zinc-500 mb-4">AS${_state.asn} verified via <strong>${esc(_state.challenge.mntner)}</strong>. Fill in your WireGuard details.</p>
        <form id="step4-form" class="space-y-3">
          <label class="block">
            <span class="block text-xs uppercase tracking-wider text-zinc-500 mb-1">Your WireGuard Public Key *</span>
            <input name="wg_public_key" required
                   class="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100">
          </label>
          <label class="block">
            <span class="block text-xs uppercase tracking-wider text-zinc-500 mb-1">Your Endpoint (host:port, optional)</span>
            <input name="endpoint" placeholder="e.g. example.com:51820"
                   class="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm">
          </label>
          <label class="block">
            <span class="block text-xs uppercase tracking-wider text-zinc-500 mb-1">Your Peer Link-Local Address *</span>
            <input name="peer_lla" required placeholder="e.g. fe80::1234:5678"
                   class="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm">
          </label>
          <label class="block">
            <span class="block text-xs uppercase tracking-wider text-zinc-500 mb-1">Network Backend</span>
            <select name="net_backend" class="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm">
              <option value="networkd" selected>systemd-networkd</option>
              <option value="nm">NetworkManager</option>
            </select>
          </label>
          <label class="block">
            <span class="block text-xs uppercase tracking-wider text-zinc-500 mb-1">Listen Port (blank = auto)</span>
            <input name="listen_port" type="number" min="0" max="65535"
                   class="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm">
          </label>
          <div class="flex gap-2 pt-1">
            <button type="submit" class="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90">Submit peering request</button>
          </div>
          ${err ? `<p class="text-xs text-red-600 dark:text-red-400">${esc(err)}</p>` : ""}
        </form>
      </section>`;
    $("#step4-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = Object.fromEntries(new FormData(e.target));
      const body = { wg_public_key: fd.wg_public_key, endpoint: fd.endpoint || "", peer_lla: fd.peer_lla, net_backend: fd.net_backend };
      if (fd.listen_port) body.listen_port = Number(fd.listen_port);
      try {
        const res = await post("/submit", body);
        renderSuccess(res);
      } catch (err) { renderStep4(err.message); }
    });
  }

  /* ── success ────────────────────────────────────────────── */
  function renderSuccess(res) {
    setStep(4);
    $("#wizard").innerHTML = `
      <section class="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6 text-center">
        <h2 class="text-lg font-semibold mb-2">Peering request submitted</h2>
        <p class="text-sm text-zinc-500 mb-4">${esc(res.message)}</p>
        <div class="inline-block text-left bg-white dark:bg-black border border-zinc-200 dark:border-zinc-800 rounded-lg p-4 text-sm space-y-1">
          <p><span class="text-zinc-500">Proposal ID:</span> <strong>#${res.proposal_id}</strong></p>
          <p><span class="text-zinc-500">Status:</span> ${esc(res.status)}</p>
          <p><span class="text-zinc-500">Node ID:</span> <code class="text-xs">${esc(res.node_id)}</code></p>
        </div>
        <p class="mt-6 text-xs text-zinc-500">The operator will review your request. You can close this page.</p>
        <button onclick="dn42ctlPeer.restart()" class="mt-4 text-xs underline text-zinc-500">Submit another request</button>
      </section>`;
  }

  /* ── navigation helpers ─────────────────────────────────── */
  function goStep1() { renderStep1(); }
  function goStep2() { renderStep2(); }
  function restart() { _state = { step: 1, asn: 0, mntners: [], challenge: null, session: null }; renderStep1(); }

  function boot() {
    $("#theme-toggle").addEventListener("click", themeToggle);
    renderStep1();
  }

  return { boot, pickAuth, goStep1, goStep2, restart };
})();
