import { useCallback, useEffect, useState } from "react";
import { api, API_PATHS, withNode } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { FormModal, type FieldDef } from "../../shared/components/Modal";
import { ConfirmModal } from "../../shared/components/Modal";
import { useToast } from "../../shared/components/Toast";
import { useNodeScope } from "../NodeContext";

interface BgpPeer {
  peer_asn: number;
  ifname: string;
  endpoint: string;
  peer_lla: string;
  listen_port: number;
  net_backend: string;
  peer_public_key: string;
  allowed_ips: string[];
}

const columns: Column<BgpPeer>[] = [
  { label: "ASN", get: (r) => r.peer_asn },
  { label: "Interface", get: (r) => r.ifname },
  { label: "Endpoint", get: (r) => r.endpoint || "—" },
  { label: "Peer LLA", get: (r) => r.peer_lla },
  { label: "Port", get: (r) => r.listen_port },
  { label: "AllowedIPs", get: (r) => (r.allowed_ips || []).join(", ") },
  { label: "Backend", get: (r) => r.net_backend },
];

/** Blank stays absent from the body so the server keeps the existing list. */
function parseAllowedIps(raw: string): string[] | undefined {
  const items = raw.split(",").map((s) => s.trim()).filter(Boolean);
  return items.length ? items : undefined;
}

export function Bgp() {
  const { nodeId } = useNodeScope();
  const [rows, setRows] = useState<BgpPeer[]>([]);
  const [error, setError] = useState("");
  const [modal, setModal] = useState<"add" | "edit" | "delete" | null>(null);
  const [selected, setSelected] = useState<BgpPeer | null>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      setRows(await api<BgpPeer[]>(withNode(`${API_PATHS.bgpPeers}?live=false`, nodeId)));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, [nodeId]);

  useEffect(() => { load(); }, [load]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;

  const addFields: FieldDef[] = [
    { name: "peer_asn", label: "Peer ASN", type: "number", required: true },
    { name: "peer_public_key", label: "WG Public Key", required: true },
    { name: "endpoint", label: "Endpoint (host:port)" },
    { name: "peer_lla", label: "Peer LLA (IPv6)", required: true },
    { name: "allowed_ips", label: "AllowedIPs (comma separated, blank=default)" },
    { name: "listen_port", label: "Listen port (blank=auto)", type: "number" },
  ];

  const editFields = (p: BgpPeer): FieldDef[] => [
    { name: "peer_public_key", label: "WG Public Key", value: p.peer_public_key, required: true },
    { name: "endpoint", label: "Endpoint", value: p.endpoint || "" },
    { name: "peer_lla", label: "Peer LLA", value: p.peer_lla, required: true },
    { name: "allowed_ips", label: "AllowedIPs (comma separated)", value: (p.allowed_ips || []).join(", ") },
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
            };
            if (d.listen_port) body.listen_port = Number(d.listen_port);
            const allowed = parseAllowedIps(d.allowed_ips || "");
            if (allowed) body.allowed_ips = allowed;
            await api(withNode(API_PATHS.bgpPeers, nodeId), { method: "POST", body: JSON.stringify(body) });
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
            };
            if (d.listen_port) body.listen_port = Number(d.listen_port);
            const allowed = parseAllowedIps(d.allowed_ips || "");
            if (allowed) body.allowed_ips = allowed;
            await api(withNode(API_PATHS.bgpPeer(selected.peer_asn), nodeId), {
              method: "PUT",
              body: JSON.stringify(body),
            });
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
            await api(withNode(API_PATHS.bgpPeer(selected.peer_asn), nodeId), { method: "DELETE" });
            toast("Deleted");
            setModal(null);
            load();
          }}
        />
      )}
    </>
  );
}
