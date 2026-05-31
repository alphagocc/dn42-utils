import { useState } from "react";
import { AUTOPEER_API } from "../../shared/api";
import type { Challenge, SubmitResult } from "../App";

interface Props {
  asn: number;
  challenge: Challenge;
  session: string;
  onResult: (result: SubmitResult) => void;
}

export function Step4Submit({ asn, challenge, session, onResult }: Props) {
  const [error, setError] = useState("");

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = Object.fromEntries(new FormData(e.currentTarget));
    const body: Record<string, unknown> = {
      wg_public_key: fd.wg_public_key,
      endpoint: fd.endpoint || "",
      peer_lla: fd.peer_lla,
      net_backend: fd.net_backend,
    };
    if (fd.listen_port) body.listen_port = Number(fd.listen_port);

    try {
      const res = await fetch(`${AUTOPEER_API}/submit`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${session}`,
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

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
      <h2 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-2">
        Step 4 — Submit your peering info
      </h2>
      <p className="text-xs text-zinc-500 mb-4">
        AS{asn} verified via <strong>{challenge.mntner}</strong>. Fill in your WireGuard
        details.
      </p>
      <form onSubmit={handleSubmit} className="space-y-3">
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
            Network Backend
          </span>
          <select
            name="net_backend"
            className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm"
          >
            <option value="networkd">systemd-networkd</option>
            <option value="nm">NetworkManager</option>
          </select>
        </label>
        <label className="block">
          <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
            Listen Port (blank = auto)
          </span>
          <input
            name="listen_port"
            type="number"
            min="0"
            max="65535"
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
