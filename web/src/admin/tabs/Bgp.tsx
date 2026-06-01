import { useCallback, useEffect, useState } from "react";
import { api, API_PATHS } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { FormModal, type FieldDef } from "../../shared/components/Modal";
import { ConfirmModal } from "../../shared/components/Modal";
import { useToast } from "../../shared/components/Toast";

interface BgpPeer {
  peer_asn: number;
  ifname: string;
  endpoint: string;
  peer_lla: string;
  listen_port: number;
  net_backend: string;
  peer_public_key: string;
}

const columns: Column<BgpPeer>[] = [
  { label: "ASN", get: (r) => r.peer_asn },
  { label: "Interface", get: (r) => r.ifname },
  { label: "Endpoint", get: (r) => r.endpoint || "—" },
  { label: "Peer LLA", get: (r) => r.peer_lla },
  { label: "Port", get: (r) => r.listen_port },
  { label: "Backend", get: (r) => r.net_backend },
];

export function Bgp() {
  const [rows, setRows] = useState<BgpPeer[]>([]);
  const [error, setError] = useState("");
  const [modal, setModal] = useState<"add" | "edit" | "delete" | null>(null);
  const [selected, setSelected] = useState<BgpPeer | null>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      setRows(await api<BgpPeer[]>(`${API_PATHS.bgpPeers}?live=false`));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;

  const addFields: FieldDef[] = [
    { name: "peer_asn", label: "Peer ASN", type: "number", required: true },
    { name: "peer_public_key", label: "WG Public Key", required: true },
    { name: "endpoint", label: "Endpoint (host:port)" },
    { name: "peer_lla", label: "Peer LLA (IPv6)", required: true },
    { name: "net_backend", label: "Backend", type: "select", options: [{ value: "networkd", label: "networkd", selected: true }, { value: "nm", label: "NetworkManager" }] },
    { name: "listen_port", label: "Listen port (blank=auto)", type: "number" },
  ];

  const editFields = (p: BgpPeer): FieldDef[] => [
    { name: "peer_public_key", label: "WG Public Key", value: p.peer_public_key, required: true },
    { name: "endpoint", label: "Endpoint", value: p.endpoint || "" },
    { name: "peer_lla", label: "Peer LLA", value: p.peer_lla, required: true },
    { name: "net_backend", label: "Backend", type: "select", options: [{ value: "networkd", label: "networkd", selected: p.net_backend === "networkd" }, { value: "nm", label: "NetworkManager", selected: p.net_backend === "nm" }] },
    { name: "listen_port", label: "Listen port", type: "number", value: p.listen_port },
  ];

  return (
    <>
      <button
        onClick={() => setModal("add")}
        className="rounded-md bg-black dark:bg-white text-white dark:text-black px-3 py-1.5 text-xs font-medium mb-4"
      >
        + Add BGP peer
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
          title="Add BGP peer"
          fields={addFields}
          onClose={() => setModal(null)}
          onSubmit={async (d) => {
            const body: Record<string, unknown> = {
              peer_asn: Number(d.peer_asn),
              peer_public_key: d.peer_public_key,
              endpoint: d.endpoint || "",
              peer_lla: d.peer_lla,
              net_backend: d.net_backend,
            };
            if (d.listen_port) body.listen_port = Number(d.listen_port);
            await api(API_PATHS.bgpPeers, { method: "POST", body: JSON.stringify(body) });
            toast("BGP peer created");
            setModal(null);
            load();
          }}
        />
      )}

      {modal === "edit" && selected && (
        <FormModal
          title={`Edit BGP AS${selected.peer_asn}`}
          fields={editFields(selected)}
          onClose={() => setModal(null)}
          onSubmit={async (d) => {
            const body: Record<string, unknown> = {
              peer_public_key: d.peer_public_key,
              endpoint: d.endpoint || "",
              peer_lla: d.peer_lla,
              net_backend: d.net_backend,
            };
            if (d.listen_port) body.listen_port = Number(d.listen_port);
            await api(`${API_PATHS.bgpPeers}/${selected.peer_asn}`, { method: "PUT", body: JSON.stringify(body) });
            toast("BGP peer updated");
            setModal(null);
            load();
          }}
        />
      )}

      {modal === "delete" && selected && (
        <ConfirmModal
          message={`Delete BGP peer AS${selected.peer_asn}?`}
          onClose={() => setModal(null)}
          onConfirm={async () => {
            await api(`${API_PATHS.bgpPeers}/${selected.peer_asn}`, { method: "DELETE" });
            toast("Deleted");
            setModal(null);
            load();
          }}
        />
      )}
    </>
  );
}
