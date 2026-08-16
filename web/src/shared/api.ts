export const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/+$/, "");

export const AUTOPEER_API = `${API_BASE}/api/public/auto-peer`;

const ADMIN = "/api/admin";

export const API_PATHS = {
  bgpPeers: `${ADMIN}/bgp/peers`,
  bgpPeer: (asn: number) => `${ADMIN}/bgp/peers/${asn}`,
  ibgpPeers: `${ADMIN}/ibgp/peers`,
  ibgpPeer: (name: string) => `${ADMIN}/ibgp/peers/${encodeURIComponent(name)}`,
  wgTunnels: `${ADMIN}/wg/tunnels`,
  genconf: `${ADMIN}/genconf`,
  nodes: `${ADMIN}/nodes`,
  node: (nodeId: string) => `${ADMIN}/nodes/${nodeId}`,
  nodePolicy: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/policy`,
  nodeStatus: (nodeId: string) => `/api/v1/nodes/${nodeId}/status`,
  proposals: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/proposals`,
  proposalAccept: (id: number) => `${ADMIN}/proposals/${id}/accept`,
  proposalReject: (id: number) => `${ADMIN}/proposals/${id}/reject`,
  reports: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/reports`,
  reportImport: (id: number) => `${ADMIN}/reports/${id}/import`,
  revisions: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/revisions`,
  rollback: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/rollback`,
  nodeToken: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/token`,
  nodeDelete: (nodeId: string) => `${ADMIN}/nodes/${nodeId}`,
  dbTables: `${ADMIN}/db/tables`,
  dbTable: (table: string) => `${ADMIN}/db/tables/${encodeURIComponent(table)}`,
  showAll: "/api/show/all",
  version: "/api/version",
} as const;

/**
 * Append `node_id` to a path, preserving any query string it already has
 * (the peer/show routes are always called with `?live=false`).
 * An empty nodeId means "let the server resolve the default scope".
 */
export function withNode(path: string, nodeId?: string): string {
  if (!nodeId) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}node_id=${encodeURIComponent(nodeId)}`;
}

export function getToken(): string {
  return sessionStorage.getItem("dn42ctl_admin_token") || "";
}

export async function api<T = unknown>(
  path: string,
  opts: RequestInit = {},
  token?: string,
): Promise<T> {
  const t = token ?? getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(opts.headers as Record<string, string>),
  };
  if (t) headers["Authorization"] = `Bearer ${t}`;

  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });

  if (res.status === 401) {
    sessionStorage.removeItem("dn42ctl_admin_token");
    location.href = "/";
    throw new Error("unauthorized");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || JSON.stringify(body));
  }

  return res.json();
}
