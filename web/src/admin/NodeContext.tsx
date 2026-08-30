import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, API_PATHS } from "../shared/api";
import type { NodeSummary } from "../shared/components/NodeSelector";

const STORAGE_KEY = "dn42ctl_admin_node_id";

export interface ManagedNode extends NodeSummary {
  is_self: boolean;
  has_token: boolean;
  enabled: boolean;
  auto_peer: boolean;
  last_seen_at: string | null;
  write_policy: Record<string, string>;
  endpoint_host: string | null;
  own_ipv6: string | null;
  router_id: string | null;
}

interface NodeScope {
  nodes: ManagedNode[];
  /** Currently scoped node. Empty string = let the server pick its default (the self node). */
  nodeId: string;
  setNodeId: (id: string) => void;
  loading: boolean;
  error: string;
  reload: () => void;
}

const Ctx = createContext<NodeScope>({
  nodes: [],
  nodeId: "",
  setNodeId: () => {},
  loading: true,
  error: "",
  reload: () => {},
});

export function useNodeScope(): NodeScope {
  return useContext(Ctx);
}

/**
 * Holds the node roster and the currently selected scope.
 *
 * IMPORTANT: mount this OUTSIDE the subtree that `Dashboard` keys on `refreshKey`.
 * That key forces a remount to refetch; if the provider were inside it, every
 * "Refresh" would reset the node selection back to the first node.
 */
export function NodeProvider({ children }: { children: React.ReactNode }) {
  const [nodes, setNodes] = useState<ManagedNode[]>([]);
  const [nodeId, setNodeIdState] = useState(() => sessionStorage.getItem(STORAGE_KEY) || "");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const setNodeId = useCallback((id: string) => {
    setNodeIdState(id);
    // Same lifetime as the admin token — a new session starts from the default scope.
    if (id) sessionStorage.setItem(STORAGE_KEY, id);
    else sessionStorage.removeItem(STORAGE_KEY);
  }, []);

  const reload = useCallback(() => {
    setLoading(true);
    api<ManagedNode[]>(API_PATHS.nodes)
      .then((ns) => {
        setNodes(ns);
        setError("");
        // A stored selection can outlive the node it points at.
        setNodeIdState((current) => {
          if (current && ns.some((n) => n.node_id === current)) return current;
          const self = ns.find((n) => n.is_self);
          const next = self?.node_id ?? ns[0]?.node_id ?? "";
          if (next) sessionStorage.setItem(STORAGE_KEY, next);
          return next;
        });
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return (
    <Ctx.Provider value={{ nodes, nodeId, setNodeId, loading, error, reload }}>
      {children}
    </Ctx.Provider>
  );
}
