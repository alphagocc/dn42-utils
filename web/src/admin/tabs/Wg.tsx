import { useEffect, useState } from "react";
import { api, API_PATHS, withNode } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { useNodeScope } from "../NodeContext";

interface WgTunnel {
  kind: string;
  ifname: string;
  peer_asn?: number;
  name?: string;
  endpoint: string;
  listen_port: number;
  net_backend: string;
}

const columns: Column<WgTunnel>[] = [
  { label: "Kind", get: (r) => r.kind },
  { label: "Interface", get: (r) => r.ifname },
  { label: "ASN / Name", get: (r) => r.peer_asn || r.name },
  { label: "Endpoint", get: (r) => r.endpoint || "—" },
  { label: "Port", get: (r) => r.listen_port },
  { label: "Backend", get: (r) => r.net_backend },
];

export function Wg() {
  const { nodeId } = useNodeScope();
  const [rows, setRows] = useState<WgTunnel[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api<WgTunnel[]>(withNode(`${API_PATHS.wgTunnels}?live=false`, nodeId))
      .then(setRows)
      .catch((e) => setError(e.message));
  }, [nodeId]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;
  if (!rows) return <p className="text-zinc-500 text-sm">Loading...</p>;

  return (
    <>
      <p className="mb-3 text-xs text-zinc-500">
        Read-only view derived from the BGP and iBGP peer tables.
      </p>
      <Table columns={columns} rows={rows} />
    </>
  );
}
