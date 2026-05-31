import { useState } from "react";
import type { Challenge, Mntner } from "../App";

const API = "/api/public/auto-peer";

interface Props {
  asn: number;
  mntners: Mntner[];
  onResult: (challenge: Challenge) => void;
  onBack: () => void;
}

export function Step2Auth({ asn, mntners, onResult, onBack }: Props) {
  const [error, setError] = useState("");

  const pickAuth = async (mntner: string, index: number) => {
    try {
      const res = await fetch(`${API}/challenge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asn, mntner, auth_index: index }),
      });
      const json = await res.json().catch(() => ({ detail: res.statusText }));
      if (!res.ok) throw new Error(json.detail || JSON.stringify(json));
      onResult({ ...json, mntner });
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const rows = mntners.flatMap((m) =>
    m.auth_options.map((opt) => ({ mntner: m.name, ...opt })),
  );

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
      <h2 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-2">
        Step 2 — Choose authentication
      </h2>
      <p className="text-xs text-zinc-500 mb-4">AS{asn} — click a row to proceed.</p>

      {rows.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr>
                <th className="text-left text-xs uppercase tracking-wider text-zinc-500 px-3 py-2">
                  Maintainer
                </th>
                <th className="text-left text-xs uppercase tracking-wider text-zinc-500 px-3 py-2">
                  Scheme
                </th>
                <th className="text-left text-xs uppercase tracking-wider text-zinc-500 px-3 py-2">
                  Fingerprint
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr
                  key={i}
                  onClick={() => pickAuth(r.mntner, r.index)}
                  className="border-t border-zinc-100 dark:border-zinc-800 cursor-pointer hover:bg-zinc-100 dark:hover:bg-zinc-800"
                >
                  <td className="px-3 py-2 text-sm">{r.mntner}</td>
                  <td className="px-3 py-2 text-sm font-mono">{r.scheme}</td>
                  <td className="px-3 py-2 text-sm">
                    {r.fingerprint ? r.fingerprint.slice(0, 16) + "..." : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-sm text-zinc-500 mt-2">No supported auth methods found.</p>
      )}

      {error && <p className="text-xs text-red-600 dark:text-red-400 mt-2">{error}</p>}

      <button onClick={onBack} className="mt-4 text-xs underline text-zinc-500">
        Back
      </button>
    </section>
  );
}
