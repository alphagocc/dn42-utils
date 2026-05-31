import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { FormModal } from "../../shared/components/Modal";
import { useToast } from "../../shared/components/Toast";

interface Node {
  node_id: string;
  name: string;
}

interface Proposal {
  id: number;
  kind: string;
  source: string;
  status: string;
  received_at: string;
  message: string | null;
}

const columns: Column<Proposal>[] = [
  { label: "#", get: (r) => r.id },
  { label: "Kind", get: (r) => r.kind },
  { label: "Source", get: (r) => r.source },
  { label: "Status", get: (r) => r.status },
  { label: "Received", get: (r) => r.received_at },
  { label: "Message", get: (r) => r.message || "" },
];

export function Proposals() {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [nodeId, setNodeId] = useState("");
  const [rows, setRows] = useState<Proposal[]>([]);
  const [error, setError] = useState("");
  const [rejectId, setRejectId] = useState<number | null>(null);
  const toast = useToast();

  useEffect(() => {
    api<Node[]>("/api/admin/nodes").then((ns) => {
      setNodes(ns);
      if (ns.length) setNodeId(ns[0].node_id);
    }).catch((e) => setError(e.message));
  }, []);

  const loadProposals = useCallback(async (nid: string) => {
    if (!nid) return;
    try {
      setRows(await api<Proposal[]>(`/api/admin/nodes/${nid}/proposals?limit=100`));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => { if (nodeId) loadProposals(nodeId); }, [nodeId, loadProposals]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;
  if (!nodes.length) return <p className="text-zinc-500 text-sm">No nodes registered.</p>;

  const accept = async (id: number) => {
    await api(`/api/admin/proposals/${id}/accept`, { method: "POST" });
    toast("Accepted");
    loadProposals(nodeId);
  };

  return (
    <>
      <NodeSelector nodes={nodes} value={nodeId} onChange={setNodeId} />
      <Table
        columns={columns}
        rows={rows}
        actions={(r) =>
          r.status === "pending" ? (
            <>
              <button
                onClick={() => accept(r.id)}
                className="rounded px-2 py-0.5 text-xs border border-zinc-300 dark:border-zinc-700"
              >
                Accept
              </button>{" "}
              <button
                onClick={() => setRejectId(r.id)}
                className="rounded px-2 py-0.5 text-xs text-red-600 dark:text-red-400 border border-red-300 dark:border-red-700"
              >
                Reject
              </button>
            </>
          ) : null
        }
      />

      {rejectId !== null && (
        <FormModal
          title="Reject proposal"
          fields={[{ name: "reason", label: "Reason", required: true }]}
          onClose={() => setRejectId(null)}
          onSubmit={async (d) => {
            await api(`/api/admin/proposals/${rejectId}/reject`, { method: "POST", body: JSON.stringify(d) });
            toast("Rejected");
            setRejectId(null);
            loadProposals(nodeId);
          }}
        />
      )}
    </>
  );
}

function NodeSelector({ nodes, value, onChange }: { nodes: Node[]; value: string; onChange: (v: string) => void }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="mb-3 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-2 py-1 text-sm"
    >
      {nodes.map((n) => (
        <option key={n.node_id} value={n.node_id}>
          {n.name} ({n.node_id.slice(0, 8)})
        </option>
      ))}
    </select>
  );
}
