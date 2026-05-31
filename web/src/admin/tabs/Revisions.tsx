import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { useToast } from "../../shared/components/Toast";

interface Node {
  node_id: string;
  name: string;
}

interface Revision {
  id: number;
  revision: string;
  generated_at: string;
}

interface RevisionData {
  pinned_revision: string | null;
  revisions: Revision[];
}

const columns: Column<Revision>[] = [
  { label: "#", get: (r) => r.id },
  { label: "Revision", get: (r) => r.revision },
  { label: "Generated", get: (r) => r.generated_at },
];

export function Revisions() {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [nodeId, setNodeId] = useState("");
  const [data, setData] = useState<RevisionData | null>(null);
  const [error, setError] = useState("");
  const toast = useToast();

  useEffect(() => {
    api<Node[]>("/api/admin/nodes").then((ns) => {
      setNodes(ns);
      if (ns.length) setNodeId(ns[0].node_id);
    }).catch((e) => setError(e.message));
  }, []);

  const loadRevisions = useCallback(async (nid: string) => {
    if (!nid) return;
    try {
      setData(await api<RevisionData>(`/api/admin/nodes/${nid}/revisions?limit=50`));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => { if (nodeId) loadRevisions(nodeId); }, [nodeId, loadRevisions]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;
  if (!nodes.length) return <p className="text-zinc-500 text-sm">No nodes registered.</p>;

  const pin = async (rev: string) => {
    await api(`/api/admin/nodes/${nodeId}/rollback`, { method: "POST", body: JSON.stringify({ revision: rev }) });
    toast("Pinned");
    loadRevisions(nodeId);
  };

  const unpin = async () => {
    await api(`/api/admin/nodes/${nodeId}/rollback`, { method: "DELETE" });
    toast("Unpinned");
    loadRevisions(nodeId);
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

      {data?.pinned_revision && (
        <p className="text-sm mb-2">
          Pinned: <code>{data.pinned_revision}</code>{" "}
          <button onClick={unpin} className="text-xs underline">
            Unpin
          </button>
        </p>
      )}

      {data && (
        <Table
          columns={columns}
          rows={data.revisions}
          actions={(r) =>
            r.revision !== data.pinned_revision ? (
              <button
                onClick={() => pin(r.revision)}
                className="rounded px-2 py-0.5 text-xs border border-zinc-300 dark:border-zinc-700"
              >
                Pin
              </button>
            ) : (
              <span className="text-xs text-zinc-500">pinned</span>
            )
          }
        />
      )}
    </>
  );
}
