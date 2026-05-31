import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";

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
  const [rows, setRows] = useState<WgTunnel[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api<WgTunnel[]>("/api/wg/tunnels?live=false")
      .then(setRows)
      .catch((e) => setError(e.message));
  }, []);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;
  if (!rows) return <p className="text-zinc-500 text-sm">Loading...</p>;

  return <Table columns={columns} rows={rows} />;
}
