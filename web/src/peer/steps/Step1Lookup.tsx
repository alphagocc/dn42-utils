import { type FormEvent, useState } from "react";
import { AUTOPEER_API } from "../../shared/api";
import type { Mntner } from "../App";

interface Props {
  asn: number;
  onResult: (asn: number, mntners: Mntner[]) => void;
}

export function Step1Lookup({ asn, onResult }: Props) {
  const [error, setError] = useState("");

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const value = Number(new FormData(e.currentTarget).get("asn"));
    try {
      const res = await fetch(`${AUTOPEER_API}/lookup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asn: value }),
      });
      const json = await res.json().catch(() => ({ detail: res.statusText }));
      if (!res.ok) throw new Error(json.detail || JSON.stringify(json));
      onResult(value, json.mntners);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
      <h2 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-4">
        Step 1 — Identify your AS
      </h2>
      <form onSubmit={handleSubmit} className="space-y-4">
        <label className="block">
          <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
            Your ASN
          </span>
          <input
            name="asn"
            type="number"
            min="1"
            required
            placeholder="e.g. 4242421234"
            defaultValue={asn || ""}
            className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100"
          />
        </label>
        <button
          type="submit"
          className="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90"
        >
          Look up
        </button>
        {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      </form>
    </section>
  );
}
