export const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/+$/, "");

export const AUTOPEER_API = `${API_BASE}/api/public/auto-peer`;

const ADMIN = "/api/admin";

export const API_PATHS = {
  bgpPeers: `${ADMIN}/bgp/peers`,
  ibgpPeers: `${ADMIN}/ibgp/peers`,
  wgTunnels: `${ADMIN}/wg/tunnels`,
  genconf: `${ADMIN}/genconf`,
  nodes: `${ADMIN}/nodes`,
  proposals: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/proposals`,
  proposalAccept: (id: number) => `${ADMIN}/proposals/${id}/accept`,
  proposalReject: (id: number) => `${ADMIN}/proposals/${id}/reject`,
  reports: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/reports`,
  reportImport: (id: number) => `${ADMIN}/reports/${id}/import`,
  revisions: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/revisions`,
  rollback: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/rollback`,
  nodeToken: (nodeId: string) => `${ADMIN}/nodes/${nodeId}/token`,
  nodeDelete: (nodeId: string) => `${ADMIN}/nodes/${nodeId}`,
  showAll: "/api/show/all",
  version: "/api/version",
} as const;

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
