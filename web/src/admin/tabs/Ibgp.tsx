import { useCallback, useEffect, useState } from "react";
import { api, API_PATHS } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { FormModal, ConfirmModal, type FieldDef } from "../../shared/components/Modal";
import { useToast } from "../../shared/components/Toast";

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
}

const columns: Column<IbgpPeer>[] = [
  { label: "Name", get: (r) => r.name },
  { label: "Interface", get: (r) => r.ifname },
  { label: "Peer IP", get: (r) => r.peer_ip },
  { label: "Endpoint", get: (r) => r.endpoint || "—" },
  { label: "rxcost", get: (r) => r.babel_rxcost },
  { label: "Type", get: (r) => r.babel_type },
  { label: "WG", get: (r) => r.has_wg ? "yes" : "no" },
];

export function Ibgp() {
  const [rows, setRows] = useState<IbgpPeer[]>([]);
  const [error, setError] = useState("");
  const [modal, setModal] = useState<"add" | "edit" | "delete" | null>(null);
  const [selected, setSelected] = useState<IbgpPeer | null>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      setRows(await api<IbgpPeer[]>(`${API_PATHS.ibgpPeers}?live=false`));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;

  const addFields: FieldDef[] = [
    { name: "name", label: "Name", required: true },
    { name: "peer_ip", label: "Peer IP (in-net IPv6)", required: true },
    { name: "has_wg", label: "WireGuard tunnel", type: "checkbox", value: true },
    { name: "peer_public_key", label: "WG Public Key" },
    { name: "endpoint", label: "Endpoint" },
    { name: "peer_lla", label: "Peer LLA" },
    { name: "net_backend", label: "Backend", type: "select", options: [{ value: "networkd", label: "networkd", selected: true }, { value: "nm", label: "NetworkManager" }] },
    { name: "babel_rxcost", label: "Babel rxcost", type: "number", value: 0 },
    { name: "babel_type", label: "Babel type", type: "select", options: [{ value: "tunnel", label: "tunnel", selected: true }, { value: "wired", label: "wired" }, { value: "wireless", label: "wireless" }] },
    { name: "listen_port", label: "Listen port", type: "number" },
  ];

  const editFields = (p: IbgpPeer): FieldDef[] => [
    { name: "peer_ip", label: "Peer IP", value: p.peer_ip, required: true },
    { name: "peer_public_key", label: "WG Public Key", value: p.peer_public_key || "", required: true },
    { name: "endpoint", label: "Endpoint", value: p.endpoint || "" },
    { name: "peer_lla", label: "Peer LLA", value: p.peer_lla || "" },
    { name: "net_backend", label: "Backend", type: "select", options: [{ value: "networkd", label: "networkd", selected: p.net_backend === "networkd" }, { value: "nm", label: "NetworkManager", selected: p.net_backend === "nm" }] },
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
              net_backend: d.net_backend,
              babel_rxcost: Number(d.babel_rxcost || 0),
              babel_type: d.babel_type,
            };
            if (d.peer_public_key) body.peer_public_key = d.peer_public_key;
            if (d.endpoint) body.endpoint = d.endpoint;
            if (d.peer_lla) body.peer_lla = d.peer_lla;
            if (d.listen_port) body.listen_port = Number(d.listen_port);
            await api(API_PATHS.ibgpPeers, { method: "POST", body: JSON.stringify(body) });
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
              net_backend: d.net_backend,
              babel_rxcost: Number(d.babel_rxcost || 120),
              babel_type: d.babel_type,
            };
            if (d.listen_port) body.listen_port = Number(d.listen_port);
            await api(`${API_PATHS.ibgpPeers}/${selected.name}`, { method: "PUT", body: JSON.stringify(body) });
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
            await api(`${API_PATHS.ibgpPeers}/${selected.name}`, { method: "DELETE" });
            toast("Deleted");
            setModal(null);
            load();
          }}
        />
      )}
    </>
  );
}
