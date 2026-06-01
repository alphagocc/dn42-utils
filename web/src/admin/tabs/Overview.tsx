import { useEffect, useState } from "react";
import { api, API_PATHS } from "../../shared/api";

interface OverviewData {
  node_id: string;
  bgp: unknown[];
  ibgp: unknown[];
  wg: unknown[];
}

export function Overview() {
  const [data, setData] = useState<OverviewData | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<OverviewData>(`${API_PATHS.showAll}?live=false`)
      .then(setData)
      .catch((e) => setError(e.message));
  }, []);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;
  if (!data) return <p className="text-zinc-500 text-sm">Loading...</p>;

  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <Card label="Node" value={data.node_id} />
      <Card label="Peers" value={`${data.bgp.length} BGP / ${data.ibgp.length} iBGP`} />
      <Card label="WG tunnels" value={String(data.wg.length)} />
    </div>
  );
}

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-4">
      <p className="text-xs uppercase tracking-wider text-zinc-500">{label}</p>
      <p className="mt-1 text-lg font-semibold">{value}</p>
    </div>
  );
}
