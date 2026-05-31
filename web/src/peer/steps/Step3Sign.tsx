import { useState } from "react";
import { AUTOPEER_API } from "../../shared/api";
import type { Challenge } from "../App";

interface Props {
  asn: number;
  challenge: Challenge;
  onResult: (session: string) => void;
  onBack: () => void;
}

export function Step3Sign({ asn: _asn, challenge, onResult, onBack }: Props) {
  const [error, setError] = useState("");
  const isSSH = challenge.scheme === "ssh";

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const sig = new FormData(e.currentTarget).get("signature") as string;
    try {
      const res = await fetch(`${AUTOPEER_API}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ challenge_id: challenge.challenge_id, signature: sig }),
      });
      const json = await res.json().catch(() => ({ detail: res.statusText }));
      if (!res.ok) throw new Error(json.detail || JSON.stringify(json));
      onResult(json.peer_session_token);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
      <h2 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-2">
        Step 3 — Sign the challenge
      </h2>
      <p className="text-xs text-zinc-500 mb-4">
        Maintainer: <strong>{challenge.mntner}</strong> / Scheme:{" "}
        <strong>{challenge.scheme}</strong>
      </p>

      <div className="mb-4 rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-100 dark:bg-zinc-800 px-4 py-3">
        <p className="text-xs uppercase tracking-wider text-zinc-500 mb-1">Nonce (hex)</p>
        <code className="text-sm break-all select-all">{challenge.nonce}</code>
      </div>

      {isSSH ? (
        <>
          <p className="text-sm mb-2">Run this on your machine:</p>
          <pre className="text-xs bg-white dark:bg-black border border-zinc-200 dark:border-zinc-800 rounded p-3 overflow-x-auto select-all">
            {`echo -n "${challenge.nonce}" > /tmp/dn42-challenge.txt\nssh-keygen -Y sign -n ${challenge.namespace} -f ~/.ssh/id_ed25519 /tmp/dn42-challenge.txt\ncat /tmp/dn42-challenge.txt.sig`}
          </pre>
          <p className="text-xs text-zinc-500 mt-1">
            Use the key that matches your mntner's <code>auth:</code> line. Adjust{" "}
            <code>~/.ssh/id_ed25519</code> to your actual key path.
          </p>
        </>
      ) : (
        <>
          <p className="text-sm mb-2">Run this on your machine:</p>
          <pre className="text-xs bg-white dark:bg-black border border-zinc-200 dark:border-zinc-800 rounded p-3 overflow-x-auto select-all">
            {`echo -n "${challenge.nonce}" | gpg --clearsign`}
          </pre>
          <p className="text-xs text-zinc-500 mt-1">
            Use the PGP key registered in your mntner.
          </p>
        </>
      )}

      <form onSubmit={handleSubmit} className="mt-4 space-y-3">
        <label className="block">
          <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
            Paste your signature
          </span>
          <textarea
            name="signature"
            rows={8}
            required
            className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100"
          />
        </label>
        <div className="flex gap-2">
          <button
            type="submit"
            className="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90"
          >
            Verify
          </button>
          <button
            type="button"
            onClick={onBack}
            className="rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm"
          >
            Back
          </button>
        </div>
        {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      </form>
    </section>
  );
}
