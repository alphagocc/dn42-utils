import { useCallback, useEffect, useState } from "react";
import { api, API_PATHS } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { useToast } from "../../shared/components/Toast";

interface Node {
  node_id: string;
  name: string;
}

interface Report {
  id: number;
  kind: string;
  received_at: string;
  imported_at: string | null;
  payload: unknown;
}

const columns: Column<Report>[] = [
  { label: "#", get: (r) => r.id },
  { label: "Kind", get: (r) => r.kind },
  { label: "Received", get: (r) => r.received_at },
  { label: "Imported", get: (r) => r.imported_at || "—" },
  { label: "Payload", get: (r) => r.payload },
];

export function Reports() {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [nodeId, setNodeId] = useState("");
  const [rows, setRows] = useState<Report[]>([]);
  const [error, setError] = useState("");
  const toast = useToast();

  useEffect(() => {
    api<Node[]>(API_PATHS.nodes).then((ns) => {
      setNodes(ns);
      if (ns.length) setNodeId(ns[0].node_id);
    }).catch((e) => setError(e.message));
  }, []);

  const loadReports = useCallback(async (nid: string) => {
    if (!nid) return;
    try {
      setRows(await api<Report[]>(`${API_PATHS.reports(nid)}?limit=50`));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => { if (nodeId) loadReports(nodeId); }, [nodeId, loadReports]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;
  if (!nodes.length) return <p className="text-zinc-500 text-sm">No nodes registered.</p>;

  const importReport = async (id: number) => {
    await api(API_PATHS.reportImport(id), { method: "POST" });
    toast("Imported");
    loadReports(nodeId);
  };

  return (
    <>
      <select
        value={nodeId}
        onChange={(e) => setNodeId(e.target.value)}
        className="mb-3 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-2 py-1 text-sm"
      >
        {nodes.map((n) => (
          <option key={n.node_id} value={n.node_id}>
            {n.name} ({n.node_id.slice(0, 8)})
          </option>
        ))}
      </select>
      <Table
        columns={columns}
        rows={rows}
        actions={(r) =>
          !r.imported_at && r.kind === "scan_result" ? (
            <button
              onClick={() => importReport(r.id)}
              className="rounded px-2 py-0.5 text-xs border border-zinc-300 dark:border-zinc-700"
            >
              Import
            </button>
          ) : null
        }
      />
    </>
  );
}
