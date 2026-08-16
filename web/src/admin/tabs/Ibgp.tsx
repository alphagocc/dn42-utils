import { useCallback, useEffect, useState } from "react";
import { api, API_PATHS, withNode } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { FormModal, ConfirmModal, type FieldDef } from "../../shared/components/Modal";
import { useToast } from "../../shared/components/Toast";
import { useNodeScope } from "../NodeContext";

interface IbgpPeer {
  name: string;
  ifname: string;
  peer_ip: string;
  endpoint: string;
  babel_rxcost: number;
  babel_type: string;
  has_wg: boolean;
  peer_public_key?: string;
  peer_lla?: string;
  net_backend: string;
  listen_port?: number;
  allowed_ips: string[];
  remote_node_id: string | null;
}

function parseAllowedIps(raw: string): string[] | undefined {
  const items = raw.split(",").map((s) => s.trim()).filter(Boolean);
  return items.length ? items : undefined;
}

export function Ibgp() {
  const { nodeId, nodes } = useNodeScope();
  const [rows, setRows] = useState<IbgpPeer[]>([]);
  const [error, setError] = useState("");
  const [modal, setModal] = useState<"add" | "edit" | "delete" | null>(null);
  const [selected, setSelected] = useState<IbgpPeer | null>(null);
  const toast = useToast();

  const nodeName = useCallback(
    (id: string | null) => {
      if (!id) return "—";
      return nodes.find((n) => n.node_id === id)?.name ?? `${id.slice(0, 8)}…`;
    },
    [nodes],
  );

  const columns: Column<IbgpPeer>[] = [
    { label: "Name", get: (r) => r.name },
    { label: "Interface", get: (r) => r.ifname },
    { label: "Peer IP", get: (r) => r.peer_ip },
    { label: "Endpoint", get: (r) => r.endpoint || "—" },
    { label: "Remote node", get: (r) => nodeName(r.remote_node_id) },
    { label: "rxcost", get: (r) => r.babel_rxcost },
    { label: "Type", get: (r) => r.babel_type },
    { label: "WG", get: (r) => (r.has_wg ? "yes" : "no") },
  ];

  const load = useCallback(async () => {
    try {
      setRows(await api<IbgpPeer[]>(withNode(`${API_PATHS.ibgpPeers}?live=false`, nodeId)));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, [nodeId]);

  useEffect(() => { load(); }, [load]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;

  /** Options for the "which managed node does this peer represent" link. */
  const remoteNodeOptions = (current: string | null) => [
    { value: "", label: "— none —", selected: !current },
    ...nodes.map((n) => ({
      value: n.node_id,
      label: `${n.name} (${n.node_id.slice(0, 8)})`,
      selected: n.node_id === current,
    })),
  ];

  const addFields: FieldDef[] = [
    { name: "name", label: "Name", required: true },
    { name: "peer_ip", label: "Peer IP (in-net IPv6)", required: true },
    { name: "has_wg", label: "WireGuard tunnel", type: "checkbox", value: true },
    { name: "peer_public_key", label: "WG Public Key" },
    { name: "endpoint", label: "Endpoint" },
    { name: "peer_lla", label: "Peer LLA" },
    { name: "remote_node_id", label: "Remote node (for address propagation)", type: "select", options: remoteNodeOptions(null) },
    { name: "allowed_ips", label: "AllowedIPs (comma separated, blank=default)" },
    { name: "babel_rxcost", label: "Babel rxcost", type: "number", value: 0 },
    { name: "babel_type", label: "Babel type", type: "select", options: [{ value: "tunnel", label: "tunnel", selected: true }, { value: "wired", label: "wired" }, { value: "wireless", label: "wireless" }] },
    { name: "listen_port", label: "Listen port", type: "number" },
  ];

  const editFields = (p: IbgpPeer): FieldDef[] => [
    { name: "peer_ip", label: "Peer IP", value: p.peer_ip, required: true },
    { name: "peer_public_key", label: "WG Public Key", value: p.peer_public_key || "", required: true },
    { name: "endpoint", label: "Endpoint", value: p.endpoint || "" },
    { name: "peer_lla", label: "Peer LLA", value: p.peer_lla || "" },
    { name: "remote_node_id", label: "Remote node (for address propagation)", type: "select", options: remoteNodeOptions(p.remote_node_id) },
    { name: "allowed_ips", label: "AllowedIPs (comma separated)", value: (p.allowed_ips || []).join(", ") },
    { name: "babel_rxcost", label: "rxcost", type: "number", value: p.babel_rxcost },
    { name: "babel_type", label: "Babel type", type: "select", options: [{ value: "tunnel", label: "tunnel", selected: p.babel_type === "tunnel" }, { value: "wired", label: "wired", selected: p.babel_type === "wired" }, { value: "wireless", label: "wireless", selected: p.babel_type === "wireless" }] },
    { name: "listen_port", label: "Listen port", type: "number", value: p.listen_port },
  ];

  return (
    <>
      <button
        onClick={() => setModal("add")}
        className="rounded-md bg-black dark:bg-white text-white dark:text-black px-3 py-1.5 text-xs font-medium mb-4"
      >
        + Add iBGP peer
      </button>

      <Table
        columns={columns}
        rows={rows}
        actions={(r) => (
          <>
            <button
              onClick={() => { setSelected(r); setModal("edit"); }}
              className="rounded px-2 py-0.5 text-xs border border-zinc-300 dark:border-zinc-700"
            >
              Edit
            </button>{" "}
            <button
              onClick={() => { setSelected(r); setModal("delete"); }}
              className="rounded px-2 py-0.5 text-xs text-red-600 dark:text-red-400 border border-red-300 dark:border-red-700"
            >
              Delete
            </button>
          </>
        )}
      />

      {modal === "add" && (
        <FormModal
          title="Add iBGP peer"
          fields={addFields}
          onClose={() => setModal(null)}
          onSubmit={async (d) => {
            const body: Record<string, unknown> = {
              name: d.name,
              peer_ip: d.peer_ip,
              has_wg: !!d.has_wg,
              babel_rxcost: Number(d.babel_rxcost || 0),
              babel_type: d.babel_type,
              // The select always submits; "" is how the UI says "not linked".
              remote_node_id: d.remote_node_id || null,
            };
            if (d.peer_public_key) body.peer_public_key = d.peer_public_key;
            if (d.endpoint) body.endpoint = d.endpoint;
            if (d.peer_lla) body.peer_lla = d.peer_lla;
            if (d.listen_port) body.listen_port = Number(d.listen_port);
            const allowed = parseAllowedIps(d.allowed_ips || "");
            if (allowed) body.allowed_ips = allowed;
            await api(withNode(API_PATHS.ibgpPeers, nodeId), { method: "POST", body: JSON.stringify(body) });
            toast("iBGP peer created");
            setModal(null);
            load();
          }}
        />
      )}

      {modal === "edit" && selected && (
        <FormModal
          title={`Edit iBGP ${selected.name}`}
          fields={editFields(selected)}
          onClose={() => setModal(null)}
          onSubmit={async (d) => {
            const body: Record<string, unknown> = {
              peer_public_key: d.peer_public_key,
              endpoint: d.endpoint || "",
              peer_lla: d.peer_lla || "",
              peer_ip: d.peer_ip,
              babel_rxcost: Number(d.babel_rxcost || 120),
              babel_type: d.babel_type,
              remote_node_id: d.remote_node_id || null,
            };
            if (d.listen_port) body.listen_port = Number(d.listen_port);
            const allowed = parseAllowedIps(d.allowed_ips || "");
            if (allowed) body.allowed_ips = allowed;
            await api(withNode(API_PATHS.ibgpPeer(selected.name), nodeId), {
              method: "PUT",
              body: JSON.stringify(body),
            });
            toast("iBGP peer updated");
            setModal(null);
            load();
          }}
        />
      )}

      {modal === "delete" && selected && (
        <ConfirmModal
          message={`Delete iBGP peer ${selected.name}?`}
          onClose={() => setModal(null)}
          onConfirm={async () => {
            await api(withNode(API_PATHS.ibgpPeer(selected.name), nodeId), { method: "DELETE" });
            toast("Deleted");
            setModal(null);
            load();
          }}
        />
      )}
    </>
  );
}
