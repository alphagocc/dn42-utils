import { useCallback, useEffect, useState } from "react";
import { api, API_PATHS } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { FormModal, ConfirmModal } from "../../shared/components/Modal";
import { Modal } from "../../shared/components/Modal";
import { useToast } from "../../shared/components/Toast";

interface Node {
  node_id: string;
  name: string;
  is_self: boolean;
  has_token: boolean;
  enabled: boolean;
  last_seen_at: string | null;
}

const columns: Column<Node>[] = [
  { label: "ID", get: (r) => r.node_id.slice(0, 8) + "..." },
  { label: "Name", get: (r) => r.name },
  { label: "Self", get: (r) => r.is_self ? "yes" : "" },
  { label: "Token", get: (r) => r.has_token ? "set" : "none" },
  { label: "Enabled", get: (r) => r.enabled ? "yes" : "no" },
  { label: "Last seen", get: (r) => r.last_seen_at || "never" },
];

export function Nodes() {
  const [rows, setRows] = useState<Node[]>([]);
  const [error, setError] = useState("");
  const [modal, setModal] = useState<"add" | "rotate" | "delete" | "token" | null>(null);
  const [selected, setSelected] = useState<Node | null>(null);
  const [newToken, setNewToken] = useState("");
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      setRows(await api<Node[]>(API_PATHS.nodes));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;

  return (
    <>
      <button
        onClick={() => setModal("add")}
        className="rounded-md bg-black dark:bg-white text-white dark:text-black px-3 py-1.5 text-xs font-medium mb-4"
      >
        + Add node
      </button>

      <Table
        columns={columns}
        rows={rows}
        actions={(r) => (
          <>
            <button
              onClick={() => { setSelected(r); setModal("rotate"); }}
              className="rounded px-2 py-0.5 text-xs border border-zinc-300 dark:border-zinc-700"
            >
              Rotate token
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
          title="Add managed node"
          fields={[
            { name: "node_id", label: "Node ID (UUID)", required: true },
            { name: "name", label: "Display name", required: true },
          ]}
          onClose={() => setModal(null)}
          onSubmit={async (d) => {
            await api(API_PATHS.nodes, { method: "POST", body: JSON.stringify(d) });
            toast("Node added");
            setModal(null);
            load();
          }}
        />
      )}

      {modal === "rotate" && selected && (
        <ConfirmModal
          message={`Rotate token for ${selected.node_id.slice(0, 8)}...? The old token will be invalidated.`}
          onClose={() => setModal(null)}
          onConfirm={async () => {
            const res = await api<{ token: string }>(API_PATHS.nodeToken(selected.node_id), { method: "POST" });
            setNewToken(res.token);
            setModal("token");
          }}
        />
      )}

      {modal === "token" && (
        <Modal onClose={() => setModal(null)}>
          <h3 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-4">
            New token (shown once)
          </h3>
          <pre className="text-xs bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded p-3 break-all">
            {newToken}
          </pre>
          <button
            onClick={() => setModal(null)}
            className="mt-4 rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm"
          >
            Close
          </button>
        </Modal>
      )}

      {modal === "delete" && selected && (
        <ConfirmModal
          message={`Delete node ${selected.node_id.slice(0, 8)}...?${selected.is_self ? " This is the SELF node — force=true will be used." : ""}`}
          onClose={() => setModal(null)}
          onConfirm={async () => {
            const url = `${API_PATHS.nodeDelete(selected.node_id)}${selected.is_self ? "?force=true" : ""}`;
            await api(url, { method: "DELETE" });
            toast("Node deleted");
            setModal(null);
            load();
          }}
        />
      )}
    </>
  );
}
