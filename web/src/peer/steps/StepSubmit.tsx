import { useEffect, useState } from "react";
import { AUTOPEER_API } from "../../shared/api";
import type { SubmitResult, Verified } from "../App";

interface PeerNode {
  node_id: string;
  name: string;
  endpoint_host: string | null;
}

interface Props {
  verified: Verified;
  onResult: (result: SubmitResult) => void;
}

export function StepSubmit({ verified, onResult }: Props) {
  const [error, setError] = useState("");
  const [nodes, setNodes] = useState<PeerNode[] | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(`${AUTOPEER_API}/nodes`);
        const json = await res.json().catch(() => ({ detail: res.statusText }));
        if (!res.ok) throw new Error(json.detail || JSON.stringify(json));
        setNodes(json.nodes);
      } catch (e) {
        setError((e as Error).message);
        setNodes([]);
      }
    })();
  }, []);

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = Object.fromEntries(new FormData(e.currentTarget));
    const body: Record<string, unknown> = {
      node_id: fd.node_id,
      wg_public_key: fd.wg_public_key,
      endpoint: fd.endpoint || "",
      peer_lla: fd.peer_lla,
    };
    if (fd.listen_port) body.listen_port = Number(fd.listen_port);

    try {
      const res = await fetch(`${AUTOPEER_API}/submit`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${verified.session}`,
        },
        body: JSON.stringify(body),
      });
      const json = await res.json().catch(() => ({ detail: res.statusText }));
      if (!res.ok) throw new Error(json.detail || JSON.stringify(json));
      onResult(json);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (nodes !== null && nodes.length === 0) {
    return (
      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
        <h2 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-2">
          Step 2 — Submit your peering info
        </h2>
        <p className="text-sm text-zinc-500">
          No node is currently open for auto-peering. Please contact the operator.
        </p>
        {error && <p className="text-xs text-red-600 dark:text-red-400 mt-4">{error}</p>}
      </section>
    );
  }

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
      <h2 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-2">
        Step 2 — Submit your peering info
      </h2>
      <p className="text-xs text-zinc-500 mb-4">
        AS{verified.asn} verified via <strong>{verified.mntner}</strong>. Pick a node and
        fill in your WireGuard details.
      </p>
      <form onSubmit={handleSubmit} className="space-y-3">
        <label className="block">
          <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
            Peer with *
          </span>
          <select
            name="node_id"
            required
            disabled={nodes === null}
            className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100"
          >
            {nodes === null ? (
              <option>Loading…</option>
            ) : (
              nodes.map((n) => (
                <option key={n.node_id} value={n.node_id}>
                  {n.endpoint_host ? `${n.name} — ${n.endpoint_host}` : n.name}
                </option>
              ))
            )}
          </select>
        </label>
        <label className="block">
          <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
            Your WireGuard Public Key *
          </span>
          <input
            name="wg_public_key"
            required
            className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100"
          />
        </label>
        <label className="block">
          <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
            Your Endpoint (host:port, optional)
          </span>
          <input
            name="endpoint"
            placeholder="e.g. example.com:51820"
            className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm"
          />
        </label>
        <label className="block">
          <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
            Your Peer Link-Local Address *
          </span>
          <input
            name="peer_lla"
            required
            placeholder="e.g. fe80::1234:5678"
            className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm"
          />
        </label>
        <label className="block">
          <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
            Listen Port (blank = auto)
          </span>
          <input
            name="listen_port"
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm"
          />
        </label>
        <div className="flex gap-2 pt-1">
          <button
            type="submit"
            className="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90"
          >
            Submit peering request
          </button>
        </div>
        {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      </form>
    </section>
  );
}
