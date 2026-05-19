/* dn42ctl admin — vanilla JS SPA */
"use strict";
const dn42ctlAdmin = (() => {
  const API = "/api";
  const ADMIN = "/api/admin";
  let _token = "";

  /* ── helpers ─────────────────────────────────────────────── */
  function token() { return _token || sessionStorage.getItem("dn42ctl_admin_token") || ""; }

  async function api(path, opts = {}) {
    const headers = { "Authorization": `Bearer ${token()}`, "Content-Type": "application/json", ...(opts.headers || {}) };
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) { sessionStorage.removeItem("dn42ctl_admin_token"); location.href = "index.html"; throw new Error("unauthorized"); }
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(body.detail || JSON.stringify(body));
    }
    return res.json();
  }

  function $(sel, root = document) { return root.querySelector(sel); }
  function $$(sel, root = document) { return [...root.querySelectorAll(sel)]; }

  function toast(msg, ok = true) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = `fixed top-4 right-4 z-50 max-w-sm rounded-md border px-4 py-3 text-sm shadow-lg ${ok ? "border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black" : "border-red-400 dark:border-red-600 bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300"}`;
    setTimeout(() => { el.className = "hidden"; }, 3500);
  }

  function themeToggle() {
    const html = document.documentElement;
    const isDark = html.classList.toggle("dark");
    localStorage.setItem("theme", isDark ? "dark" : "light");
  }

  /* ── table builder ──────────────────────────────────────── */
  function table(columns, rows, actions) {
    if (!rows.length) return `<p class="text-zinc-500 text-sm py-4">No data.</p>`;
    const hdr = columns.map(c => `<th class="text-left text-xs uppercase tracking-wider text-zinc-500 px-3 py-2 font-medium">${c.label}</th>`).join("") + (actions ? "<th></th>" : "");
    const body = rows.map(r => {
      const cells = columns.map(c => `<td class="px-3 py-2 text-sm">${esc(c.get(r))}</td>`).join("");
      const act = actions ? `<td class="px-3 py-2 text-sm text-right whitespace-nowrap">${actions(r)}</td>` : "";
      return `<tr class="border-t border-zinc-100 dark:border-zinc-800">${cells}${act}</tr>`;
    }).join("");
    return `<div class="overflow-x-auto"><table class="w-full"><thead><tr>${hdr}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function esc(v) {
    if (v === null || v === undefined) return "—";
    const escape = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    if (typeof v === "object") return `<code class="text-xs">${escape(JSON.stringify(v))}</code>`;
    return escape(v);
  }

  function btn(label, cls, onclick) {
    return `<button onclick="${onclick}" class="rounded px-2 py-0.5 text-xs ${cls}">${label}</button>`;
  }

  /* ── modal ──────────────────────────────────────────────── */
  function openModal(html) {
    $("#modal-card").innerHTML = html;
    $("#modal-root").classList.remove("hidden");
  }
  function closeModal() { $("#modal-root").classList.add("hidden"); }

  function formModal(title, fields, onSubmit) {
    const id = "modal-form-" + Date.now();
    const flds = fields.map(f => `
      <label class="block">
        <span class="block text-xs uppercase tracking-wider text-zinc-500 mb-1">${f.label}${f.required ? " *" : ""}</span>
        ${f.type === "select"
          ? `<select name="${f.name}" ${f.required ? "required" : ""} class="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm">${f.options.map(o => `<option value="${o.value}" ${o.selected ? "selected" : ""}>${o.label}</option>`).join("")}</select>`
          : f.type === "checkbox"
            ? `<input type="checkbox" name="${f.name}" ${f.value ? "checked" : ""} class="rounded border-zinc-300 dark:border-zinc-700">`
            : `<input type="${f.type || "text"}" name="${f.name}" value="${esc(f.value || "")}" ${f.required ? "required" : ""} class="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm">`
        }
      </label>`).join("");
    openModal(`
      <h3 class="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-4">${title}</h3>
      <form id="${id}" class="space-y-3">${flds}
        <div class="flex gap-2 pt-2">
          <button type="submit" class="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90">Save</button>
          <button type="button" onclick="dn42ctlAdmin.closeModal()" class="rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm">Cancel</button>
        </div>
      </form>
    `);
    $(`#${id}`).addEventListener("submit", async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(e.target));
      try { await onSubmit(data); closeModal(); } catch (err) { toast(err.message, false); }
    });
  }

  function confirmModal(msg, onConfirm) {
    openModal(`
      <p class="text-sm mb-4">${msg}</p>
      <div class="flex gap-2">
        <button id="modal-yes" class="rounded-md bg-red-600 text-white px-4 py-2 text-sm font-medium hover:opacity-90">Confirm</button>
        <button onclick="dn42ctlAdmin.closeModal()" class="rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm">Cancel</button>
      </div>
    `);
    $("#modal-yes").addEventListener("click", async () => {
      try { await onConfirm(); closeModal(); } catch (err) { toast(err.message, false); }
    });
  }

  /* ── tab renderers ──────────────────────────────────────── */
  let _currentTab = "overview";
  let _nodesCache = [];

  const tabs = {
    async overview() {
      const d = await api(`${API}/show/all?live=false`);
      return `<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div class="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-4">
          <p class="text-xs uppercase tracking-wider text-zinc-500">Node</p>
          <p class="mt-1 text-lg font-semibold">${esc(d.node_id)}</p>
        </div>
        <div class="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-4">
          <p class="text-xs uppercase tracking-wider text-zinc-500">Peers</p>
          <p class="mt-1 text-lg font-semibold">${d.bgp.length} BGP / ${d.ibgp.length} iBGP</p>
        </div>
        <div class="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-4">
          <p class="text-xs uppercase tracking-wider text-zinc-500">WG tunnels</p>
          <p class="mt-1 text-lg font-semibold">${d.wg.length}</p>
        </div>
      </div>`;
    },

    async bgp() {
      const rows = await api(`${API}/bgp/peers?live=false`);
      const addBtn = `<button onclick="dn42ctlAdmin.addBgp()" class="rounded-md bg-black dark:bg-white text-white dark:text-black px-3 py-1.5 text-xs font-medium mb-4">+ Add BGP peer</button>`;
      return addBtn + table(
        [
          { label: "ASN", get: r => r.peer_asn },
          { label: "Interface", get: r => r.ifname },
          { label: "Endpoint", get: r => r.endpoint || "—" },
          { label: "Peer LLA", get: r => r.peer_lla },
          { label: "Port", get: r => r.listen_port },
          { label: "Backend", get: r => r.net_backend },
        ],
        rows,
        r => btn("Edit", "border border-zinc-300 dark:border-zinc-700", `dn42ctlAdmin.editBgp(${r.peer_asn})`) + " " + btn("Delete", "text-red-600 dark:text-red-400 border border-red-300 dark:border-red-700", `dn42ctlAdmin.delBgp(${r.peer_asn})`)
      );
    },

    async ibgp() {
      const rows = await api(`${API}/ibgp/peers?live=false`);
      const addBtn = `<button onclick="dn42ctlAdmin.addIbgp()" class="rounded-md bg-black dark:bg-white text-white dark:text-black px-3 py-1.5 text-xs font-medium mb-4">+ Add iBGP peer</button>`;
      return addBtn + table(
        [
          { label: "Name", get: r => r.name },
          { label: "Interface", get: r => r.ifname },
          { label: "Peer IP", get: r => r.peer_ip },
          { label: "Endpoint", get: r => r.endpoint || "—" },
          { label: "rxcost", get: r => r.babel_rxcost },
          { label: "Type", get: r => r.babel_type },
          { label: "WG", get: r => r.has_wg ? "yes" : "no" },
        ],
        rows,
        r => btn("Edit", "border border-zinc-300 dark:border-zinc-700", `dn42ctlAdmin.editIbgp('${r.name}')`) + " " + btn("Delete", "text-red-600 dark:text-red-400 border border-red-300 dark:border-red-700", `dn42ctlAdmin.delIbgp('${r.name}')`)
      );
    },

    async wg() {
      const rows = await api(`${API}/wg/tunnels?live=false`);
      return table(
        [
          { label: "Kind", get: r => r.kind },
          { label: "Interface", get: r => r.ifname },
          { label: "ASN / Name", get: r => r.peer_asn || r.name },
          { label: "Endpoint", get: r => r.endpoint || "—" },
          { label: "Port", get: r => r.listen_port },
          { label: "Backend", get: r => r.net_backend },
        ],
        rows
      );
    },

    async nodes() {
      const rows = await api(`${ADMIN}/nodes`);
      _nodesCache = rows;
      const addBtn = `<button onclick="dn42ctlAdmin.addNode()" class="rounded-md bg-black dark:bg-white text-white dark:text-black px-3 py-1.5 text-xs font-medium mb-4">+ Add node</button>`;
      return addBtn + table(
        [
          { label: "ID", get: r => r.node_id.slice(0, 8) + "..." },
          { label: "Name", get: r => r.name },
          { label: "Self", get: r => r.is_self ? "yes" : "" },
          { label: "Token", get: r => r.has_token ? "set" : "none" },
          { label: "Enabled", get: r => r.enabled ? "yes" : "no" },
          { label: "Last seen", get: r => r.last_seen_at || "never" },
        ],
        rows,
        r => btn("Rotate token", "border border-zinc-300 dark:border-zinc-700", `dn42ctlAdmin.rotateToken('${r.node_id}')`) + " " + btn("Delete", "text-red-600 dark:text-red-400 border border-red-300 dark:border-red-700", `dn42ctlAdmin.delNode('${r.node_id}', ${r.is_self})`)
      );
    },

    async proposals() {
      if (!_nodesCache.length) _nodesCache = await api(`${ADMIN}/nodes`);
      const nid = selectedNodeId("prop-node") || _nodesCache[0]?.node_id;
      if (!nid) return "<p class='text-zinc-500 text-sm'>No nodes registered.</p>";
      const sel = nodeSelector("prop-node", nid);
      const rows = await api(`${ADMIN}/nodes/${nid}/proposals?limit=100`);
      return sel + table(
        [
          { label: "#", get: r => r.id },
          { label: "Kind", get: r => r.kind },
          { label: "Source", get: r => r.source },
          { label: "Status", get: r => r.status },
          { label: "Received", get: r => r.received_at },
          { label: "Message", get: r => r.message || "" },
        ],
        rows,
        r => r.status === "pending" ? btn("Accept", "border border-zinc-300 dark:border-zinc-700", `dn42ctlAdmin.acceptProposal(${r.id})`) + " " + btn("Reject", "text-red-600 dark:text-red-400 border border-red-300 dark:border-red-700", `dn42ctlAdmin.rejectProposal(${r.id})`) : ""
      );
    },

    async reports() {
      if (!_nodesCache.length) _nodesCache = await api(`${ADMIN}/nodes`);
      const nid = selectedNodeId("rep-node") || _nodesCache[0]?.node_id;
      if (!nid) return "<p class='text-zinc-500 text-sm'>No nodes registered.</p>";
      const sel = nodeSelector("rep-node", nid);
      const rows = await api(`${ADMIN}/nodes/${nid}/reports?limit=50`);
      return sel + table(
        [
          { label: "#", get: r => r.id },
          { label: "Kind", get: r => r.kind },
          { label: "Received", get: r => r.received_at },
          { label: "Imported", get: r => r.imported_at || "—" },
          { label: "Payload", get: r => r.payload },
        ],
        rows,
        r => !r.imported_at && r.kind === "scan_result" ? btn("Import", "border border-zinc-300 dark:border-zinc-700", `dn42ctlAdmin.importReport(${r.id})`) : ""
      );
    },

    async revisions() {
      if (!_nodesCache.length) _nodesCache = await api(`${ADMIN}/nodes`);
      const nid = selectedNodeId("rev-node") || _nodesCache[0]?.node_id;
      if (!nid) return "<p class='text-zinc-500 text-sm'>No nodes registered.</p>";
      const sel = nodeSelector("rev-node", nid);
      const data = await api(`${ADMIN}/nodes/${nid}/revisions?limit=50`);
      const pinned = data.pinned_revision;
      return sel + (pinned ? `<p class="text-sm mb-2">Pinned: <code>${esc(pinned)}</code> <button onclick="dn42ctlAdmin.unpin('${nid}')" class="text-xs underline">Unpin</button></p>` : "") + table(
        [
          { label: "#", get: r => r.id },
          { label: "Revision", get: r => r.revision },
          { label: "Generated", get: r => r.generated_at },
        ],
        data.revisions,
        r => r.revision !== pinned ? btn("Pin", "border border-zinc-300 dark:border-zinc-700", `dn42ctlAdmin.pin('${nid}','${r.revision}')`) : `<span class="text-xs text-zinc-500">pinned</span>`
      );
    },

    async genconf() {
      return `
        <div class="space-y-4 max-w-md">
          <p class="text-sm text-zinc-500">Regenerate Bird / Babel / ROA configuration files.</p>
          <form id="genconf-form" class="space-y-3">
            <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="overwrite_bird_conf" checked class="rounded border-zinc-300 dark:border-zinc-700"> Overwrite bird.conf</label>
            <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="overwrite_babel_conf" checked class="rounded border-zinc-300 dark:border-zinc-700"> Overwrite babel.conf</label>
            <button type="submit" class="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90">Run genconf</button>
          </form>
          <pre id="genconf-output" class="hidden text-xs bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded p-3 overflow-x-auto"></pre>
        </div>`;
    },
  };

  function nodeSelector(id, current) {
    const opts = _nodesCache.map(n => `<option value="${n.node_id}" ${n.node_id === current ? "selected" : ""}>${n.name} (${n.node_id.slice(0,8)})</option>`).join("");
    return `<select id="${id}" onchange="dn42ctlAdmin.switchTab('${_currentTab}')" class="mb-3 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-2 py-1 text-sm">${opts}</select>`;
  }

  function selectedNodeId(selectId) {
    const el = $(`#${selectId}`);
    return el ? el.value : _nodesCache[0]?.node_id;
  }

  async function switchTab(name) {
    _currentTab = name;
    $$(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    const view = $("#view");
    view.innerHTML = `<p class="text-zinc-500 text-sm">Loading...</p>`;
    try {
      view.innerHTML = await tabs[name]();
      if (name === "genconf") bindGenconf();
    } catch (err) { view.innerHTML = `<p class="text-red-600 dark:text-red-400 text-sm">${esc(err.message)}</p>`; }
  }

  /* ── genconf ────────────────────────────────────────────── */
  function bindGenconf() {
    const form = $("#genconf-form");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(form));
      try {
        const res = await api(`${API}/genconf`, { method: "POST", body: JSON.stringify({ overwrite_bird_conf: !!d.overwrite_bird_conf, overwrite_babel_conf: !!d.overwrite_babel_conf }) });
        const out = $("#genconf-output");
        out.classList.remove("hidden");
        out.textContent = JSON.stringify(res, null, 2);
        toast("genconf done");
      } catch (err) { toast(err.message, false); }
    });
  }

  /* ── BGP CRUD ───────────────────────────────────────────── */
  function addBgp() {
    formModal("Add BGP peer", [
      { name: "peer_asn", label: "Peer ASN", type: "number", required: true },
      { name: "peer_public_key", label: "WG Public Key", required: true },
      { name: "endpoint", label: "Endpoint (host:port)", required: false },
      { name: "peer_lla", label: "Peer LLA (IPv6)", required: true },
      { name: "net_backend", label: "Backend", type: "select", options: [{ value: "networkd", label: "networkd", selected: true }, { value: "nm", label: "NetworkManager" }] },
      { name: "listen_port", label: "Listen port (blank=auto)", type: "number", required: false },
    ], async (d) => {
      const body = { peer_asn: Number(d.peer_asn), peer_public_key: d.peer_public_key, endpoint: d.endpoint || "", peer_lla: d.peer_lla, net_backend: d.net_backend };
      if (d.listen_port) body.listen_port = Number(d.listen_port);
      await api(`${API}/bgp/peers`, { method: "POST", body: JSON.stringify(body) });
      toast("BGP peer created");
      switchTab("bgp");
    });
  }

  async function editBgp(asn) {
    const peers = await api(`${API}/bgp/peers?live=false`);
    const p = peers.find(r => r.peer_asn === asn);
    if (!p) { toast("Peer not found", false); return; }
    formModal(`Edit BGP AS${asn}`, [
      { name: "peer_public_key", label: "WG Public Key", value: p.peer_public_key, required: true },
      { name: "endpoint", label: "Endpoint", value: p.endpoint || "" },
      { name: "peer_lla", label: "Peer LLA", value: p.peer_lla, required: true },
      { name: "net_backend", label: "Backend", type: "select", options: [{ value: "networkd", label: "networkd", selected: p.net_backend === "networkd" }, { value: "nm", label: "NetworkManager", selected: p.net_backend === "nm" }] },
      { name: "listen_port", label: "Listen port", type: "number", value: p.listen_port },
    ], async (d) => {
      const body = { peer_public_key: d.peer_public_key, endpoint: d.endpoint || "", peer_lla: d.peer_lla, net_backend: d.net_backend };
      if (d.listen_port) body.listen_port = Number(d.listen_port);
      await api(`${API}/bgp/peers/${asn}`, { method: "PUT", body: JSON.stringify(body) });
      toast("BGP peer updated"); switchTab("bgp");
    });
  }

  function delBgp(asn) { confirmModal(`Delete BGP peer AS${asn}?`, async () => { await api(`${API}/bgp/peers/${asn}`, { method: "DELETE" }); toast("Deleted"); switchTab("bgp"); }); }

  /* ── iBGP CRUD ──────────────────────────────────────────── */
  function addIbgp() {
    formModal("Add iBGP peer", [
      { name: "name", label: "Name", required: true },
      { name: "peer_ip", label: "Peer IP (in-net IPv6)", required: true },
      { name: "has_wg", label: "WireGuard tunnel", type: "checkbox", value: true },
      { name: "peer_public_key", label: "WG Public Key" },
      { name: "endpoint", label: "Endpoint" },
      { name: "peer_lla", label: "Peer LLA" },
      { name: "net_backend", label: "Backend", type: "select", options: [{ value: "networkd", label: "networkd", selected: true }, { value: "nm", label: "NetworkManager" }] },
      { name: "babel_rxcost", label: "Babel rxcost", type: "number", value: "0" },
      { name: "babel_type", label: "Babel type", type: "select", options: [{ value: "tunnel", label: "tunnel", selected: true }, { value: "wired", label: "wired" }, { value: "wireless", label: "wireless" }] },
      { name: "listen_port", label: "Listen port", type: "number" },
    ], async (d) => {
      const body = { name: d.name, peer_ip: d.peer_ip, has_wg: !!d.has_wg, net_backend: d.net_backend, babel_rxcost: Number(d.babel_rxcost || 0), babel_type: d.babel_type };
      if (d.peer_public_key) body.peer_public_key = d.peer_public_key;
      if (d.endpoint) body.endpoint = d.endpoint;
      if (d.peer_lla) body.peer_lla = d.peer_lla;
      if (d.listen_port) body.listen_port = Number(d.listen_port);
      await api(`${API}/ibgp/peers`, { method: "POST", body: JSON.stringify(body) });
      toast("iBGP peer created"); switchTab("ibgp");
    });
  }

  async function editIbgp(name) {
    const peers = await api(`${API}/ibgp/peers?live=false`);
    const p = peers.find(r => r.name === name);
    if (!p) { toast("Peer not found", false); return; }
    formModal(`Edit iBGP ${name}`, [
      { name: "peer_ip", label: "Peer IP", value: p.peer_ip, required: true },
      { name: "peer_public_key", label: "WG Public Key", value: p.peer_public_key || "", required: true },
      { name: "endpoint", label: "Endpoint", value: p.endpoint || "" },
      { name: "peer_lla", label: "Peer LLA", value: p.peer_lla || "" },
      { name: "net_backend", label: "Backend", type: "select", options: [{ value: "networkd", label: "networkd", selected: p.net_backend === "networkd" }, { value: "nm", label: "NetworkManager", selected: p.net_backend === "nm" }] },
      { name: "babel_rxcost", label: "rxcost", type: "number", value: p.babel_rxcost },
      { name: "babel_type", label: "Babel type", type: "select", options: [{ value: "tunnel", label: "tunnel", selected: p.babel_type === "tunnel" }, { value: "wired", label: "wired", selected: p.babel_type === "wired" }, { value: "wireless", label: "wireless", selected: p.babel_type === "wireless" }] },
      { name: "listen_port", label: "Listen port", type: "number", value: p.listen_port },
    ], async (d) => {
      const body = { peer_public_key: d.peer_public_key, endpoint: d.endpoint || "", peer_lla: d.peer_lla || "", peer_ip: d.peer_ip, net_backend: d.net_backend, babel_rxcost: Number(d.babel_rxcost || 120), babel_type: d.babel_type };
      if (d.listen_port) body.listen_port = Number(d.listen_port);
      await api(`${API}/ibgp/peers/${name}`, { method: "PUT", body: JSON.stringify(body) });
      toast("iBGP peer updated"); switchTab("ibgp");
    });
  }

  function delIbgp(name) { confirmModal(`Delete iBGP peer ${name}?`, async () => { await api(`${API}/ibgp/peers/${name}`, { method: "DELETE" }); toast("Deleted"); switchTab("ibgp"); }); }

  /* ── Nodes ──────────────────────────────────────────────── */
  function addNode() {
    formModal("Add managed node", [
      { name: "node_id", label: "Node ID (UUID)", required: true },
      { name: "name", label: "Display name", required: true },
    ], async (d) => {
      await api(`${ADMIN}/nodes`, { method: "POST", body: JSON.stringify(d) });
      toast("Node added"); switchTab("nodes");
    });
  }

  async function rotateToken(nid) {
    confirmModal(`Rotate token for ${nid.slice(0,8)}...? The old token will be invalidated.`, async () => {
      const res = await api(`${ADMIN}/nodes/${nid}/token`, { method: "POST" });
      openModal(`<h3 class="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-4">New token (shown once)</h3><pre class="text-xs bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded p-3 break-all">${esc(res.token)}</pre><button onclick="dn42ctlAdmin.closeModal()" class="mt-4 rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm">Close</button>`);
    });
  }

  function delNode(nid, isSelf) {
    const url = `${ADMIN}/nodes/${nid}` + (isSelf ? "?force=true" : "");
    confirmModal(`Delete node ${nid.slice(0,8)}...?${isSelf ? " This is the SELF node — force=true will be used." : ""}`, async () => {
      await api(url, { method: "DELETE" }); toast("Node deleted"); switchTab("nodes");
    });
  }

  /* ── Proposals ──────────────────────────────────────────── */
  async function acceptProposal(id) { await api(`${ADMIN}/proposals/${id}/accept`, { method: "POST" }); toast("Accepted"); switchTab("proposals"); }
  function rejectProposal(id) {
    formModal("Reject proposal", [{ name: "reason", label: "Reason", required: true }], async (d) => {
      await api(`${ADMIN}/proposals/${id}/reject`, { method: "POST", body: JSON.stringify(d) }); toast("Rejected"); switchTab("proposals");
    });
  }

  /* ── Reports ────────────────────────────────────────────── */
  async function importReport(id) { await api(`${ADMIN}/reports/${id}/import`, { method: "POST" }); toast("Imported"); switchTab("reports"); }

  /* ── Revisions ──────────────────────────────────────────── */
  async function pin(nid, rev) { await api(`${ADMIN}/nodes/${nid}/rollback`, { method: "POST", body: JSON.stringify({ revision: rev }) }); toast("Pinned"); switchTab("revisions"); }
  async function unpin(nid) { await api(`${ADMIN}/nodes/${nid}/rollback`, { method: "DELETE" }); toast("Unpinned"); switchTab("revisions"); }

  /* ── boot ───────────────────────────────────────────────── */
  function bootLogin() {
    if (token()) { location.href = "dashboard.html"; return; }
    $("#theme-toggle").addEventListener("click", themeToggle);
    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const t = $("#token").value.trim();
      if (!t) return;
      _token = t;
      try {
        await api(`${API}/show/all?live=false`);
        sessionStorage.setItem("dn42ctl_admin_token", t);
        location.href = "dashboard.html";
      } catch {
        _token = "";
        const err = $("#login-error");
        err.textContent = "Invalid token or server unreachable.";
        err.classList.remove("hidden");
      }
    });
  }

  function bootDashboard() {
    if (!token()) { location.href = "index.html"; return; }
    $("#theme-toggle").addEventListener("click", themeToggle);
    $("#logout-btn").addEventListener("click", () => { sessionStorage.removeItem("dn42ctl_admin_token"); location.href = "index.html"; });
    $("#refresh-btn").addEventListener("click", () => switchTab(_currentTab));
    $$(".tab-btn").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
    switchTab("overview");
  }

  return {
    bootLogin, bootDashboard, switchTab, closeModal,
    addBgp, editBgp, delBgp,
    addIbgp, editIbgp, delIbgp,
    addNode, rotateToken, delNode,
    acceptProposal, rejectProposal,
    importReport, pin, unpin,
  };
})();
