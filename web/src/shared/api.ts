export const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/+$/, "");

export const AUTOPEER_API = `${API_BASE}/api/public/auto-peer`;

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
