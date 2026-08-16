import { useCallback, useEffect, useState } from "react";
import { api, API_PATHS } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";
import { FormModal, ConfirmModal, type FieldDef } from "../../shared/components/Modal";
import { Modal } from "../../shared/components/Modal";
import { useToast } from "../../shared/components/Toast";
import { useNodeScope, type ManagedNode } from "../NodeContext";

interface PropagatedChange {
  node_id: string;
  name: string;
  field: string;
  old: string | null;
  new: string;
}

interface PatchResult extends ManagedNode {
  propagated: PropagatedChange[];
  warnings: string[];
}

const columns: Column<ManagedNode>[] = [
  { label: "ID", get: (r) => r.node_id.slice(0, 8) + "..." },
  { label: "Name", get: (r) => r.name },
  { label: "Self", get: (r) => (r.is_self ? "yes" : "") },
  { label: "Token", get: (r) => (r.has_token ? "set" : "none") },
  { label: "Enabled", get: (r) => (r.enabled ? "yes" : "no") },
  { label: "Endpoint host", get: (r) => r.endpoint_host || "—" },
  { label: "own_ipv6", get: (r) => r.own_ipv6 || "—" },
  { label: "router_id", get: (r) => r.router_id || "—" },
  { label: "Last seen", get: (r) => r.last_seen_at || "never" },
];

type ModalKind = "add" | "edit" | "policy" | "status" | "rotate" | "delete" | "token" | "propagated";

/**
 * An empty text input yields "" (never absent) from FormData, and "" is how the
 * UI expresses "unmanage this field" — but the API expresses that as null.
 */
function orNull(v: string | undefined): string | null {
  const trimmed = (v || "").trim();
  return trimmed === "" ? null : trimmed;
}

export function Nodes() {
  const { nodes, reload, loading, error } = useNodeScope();
  const [modal, setModal] = useState<ModalKind | null>(null);
  const [selected, setSelected] = useState<ManagedNode | null>(null);
  const [newToken, setNewToken] = useState("");
  const [patchResult, setPatchResult] = useState<PatchResult | null>(null);
  const [statusText, setStatusText] = useState("");
  const toast = useToast();

  const open = useCallback((kind: ModalKind, node: ManagedNode) => {
    setSelected(node);
    setModal(kind);
  }, []);

  useEffect(() => {
    if (modal !== "status" || !selected) return;
    api<unknown>(API_PATHS.nodeStatus(selected.node_id))
      .then((s) => setStatusText(JSON.stringify(s, null, 2)))
      .catch((e) => setStatusText(String((e as Error).message)));
  }, [modal, selected]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;
  if (loading) return <p className="text-zinc-500 text-sm">Loading…</p>;

  const editFields = (n: ManagedNode): FieldDef[] => [
    { name: "name", label: "Display name", value: n.name, required: true },
    { name: "enabled", label: "Enabled", type: "checkbox", value: n.enabled },
    { name: "endpoint_host", label: "Endpoint host (no port; blank = not managed)", value: n.endpoint_host || "" },
    { name: "own_ipv6", label: "own_ipv6 (blank = not managed)", value: n.own_ipv6 || "" },
    { name: "router_id", label: "router_id (blank = not managed)", value: n.router_id || "" },
    {
      name: "propagate",
      label: "Propagate to other nodes' iBGP peers",
      type: "checkbox",
      value: true,
    },
  ];

  const policyFields = (n: ManagedNode): FieldDef[] => [
    {
      name: "peer_add",
      label: "peer_add",
      type: "select",
      options: [
        { value: "review", label: "review", selected: n.write_policy.peer_add === "review" },
        { value: "auto_accept", label: "auto_accept", selected: n.write_policy.peer_add === "auto_accept" },
      ],
    },
    {
      name: "report",
      label: "report",
      type: "select",
      options: [
        { value: "auto", label: "auto", selected: n.write_policy.report === "auto" },
        { value: "review", label: "review", selected: n.write_policy.report === "review" },
      ],
    },
  ];

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
        rows={nodes}
        actions={(r) => (
          <>
            <button
              onClick={() => open("edit", r)}
              className="rounded px-2 py-0.5 text-xs border border-zinc-300 dark:border-zinc-700"
            >
              Edit
            </button>{" "}
            <button
              onClick={() => open("policy", r)}
              className="rounded px-2 py-0.5 text-xs border border-zinc-300 dark:border-zinc-700"
            >
              Policy
            </button>{" "}
            <button
              onClick={() => { setStatusText("Loading…"); open("status", r); }}
              className="rounded px-2 py-0.5 text-xs border border-zinc-300 dark:border-zinc-700"
            >
              Status
            </button>{" "}
            <button
              onClick={() => open("rotate", r)}
              className="rounded px-2 py-0.5 text-xs border border-zinc-300 dark:border-zinc-700"
            >
              Rotate token
            </button>{" "}
            <button
              onClick={() => open("delete", r)}
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
            reload();
          }}
        />
      )}

      {modal === "edit" && selected && (
        <FormModal
          title={`Edit ${selected.name}`}
          fields={editFields(selected)}
          onClose={() => setModal(null)}
          onSubmit={async (d) => {
            const result = await api<PatchResult>(API_PATHS.node(selected.node_id), {
              method: "PATCH",
              body: JSON.stringify({
                name: d.name,
                // An unchecked checkbox is absent from FormData, not false.
                enabled: !!d.enabled,
                endpoint_host: orNull(d.endpoint_host),
                own_ipv6: orNull(d.own_ipv6),
                router_id: orNull(d.router_id),
                propagate: !!d.propagate,
              }),
            });
            toast("Node updated");
            reload();
            if (result.propagated.length || result.warnings.length) {
              setPatchResult(result);
              setModal("propagated");
            } else {
              setModal(null);
            }
          }}
        />
      )}

      {modal === "propagated" && patchResult && (
        <Modal onClose={() => setModal(null)}>
          <h3 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-4">
            Propagated changes
          </h3>
          {patchResult.propagated.length > 0 ? (
            <ul className="text-xs font-mono space-y-1 mb-4">
              {patchResult.propagated.map((c, i) => (
                <li key={i}>
                  {c.node_id.slice(0, 8)}/{c.name} <b>{c.field}</b>: {c.old || "—"} → {c.new}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-zinc-500 mb-4">No peer rows were rewritten.</p>
          )}
          {patchResult.warnings.length > 0 && (
            <>
              <h4 className="text-xs uppercase tracking-wider text-zinc-500 mb-2">Needs manual attention</h4>
              <ul className="text-xs space-y-1 mb-4 text-amber-700 dark:text-amber-400">
                {patchResult.warnings.map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            </>
          )}
          <button
            onClick={() => setModal(null)}
            className="rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm"
          >
            Close
          </button>
        </Modal>
      )}

      {modal === "policy" && selected && (
        <FormModal
          title={`Write policy — ${selected.name}`}
          fields={policyFields(selected)}
          onClose={() => setModal(null)}
          onSubmit={async (d) => {
            await api(API_PATHS.nodePolicy(selected.node_id), {
              method: "PATCH",
              body: JSON.stringify({ peer_add: d.peer_add, report: d.report }),
            });
            toast("Policy updated");
            setModal(null);
            reload();
          }}
        />
      )}

      {modal === "status" && selected && (
        <Modal onClose={() => setModal(null)}>
          <h3 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-4">
            Status — {selected.name}
          </h3>
          <pre className="text-xs bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded p-3 overflow-auto max-h-96">
            {statusText}
          </pre>
          <button
            onClick={() => setModal(null)}
            className="mt-4 rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm"
          >
            Close
          </button>
        </Modal>
      )}

      {modal === "rotate" && selected && (
        <ConfirmModal
          message={`Rotate token for ${selected.node_id.slice(0, 8)}...? The old token will be invalidated.`}
          onClose={() => setModal(null)}
          onConfirm={async () => {
            const res = await api<{ token: string }>(API_PATHS.nodeToken(selected.node_id), { method: "POST" });
            setNewToken(res.token);
            setModal("token");
            reload();
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
            reload();
          }}
        />
      )}
    </>
  );
}
