import { useEffect, useState } from "react";
import { api, API_PATHS, withNode } from "../../shared/api";
import { useNodeScope } from "../NodeContext";

interface OverviewData {
  node_id: string;
  self_node_id: string | null;
  config_node_id: string;
  node_id_mismatch: boolean;
  bgp: unknown[];
  ibgp: unknown[];
  wg: unknown[];
}

export function Overview() {
  const { nodeId } = useNodeScope();
  const [data, setData] = useState<OverviewData | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<OverviewData>(withNode(`${API_PATHS.showAll}?live=false`, nodeId))
      .then(setData)
      .catch((e) => setError(e.message));
  }, [nodeId]);

  if (error) return <p className="text-red-600 dark:text-red-400 text-sm">{error}</p>;
  if (!data) return <p className="text-zinc-500 text-sm">Loading...</p>;

  return (
    <>
      {data.node_id_mismatch && (
        <div className="mb-4 rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/40 p-4 text-sm">
          <p className="font-medium text-amber-800 dark:text-amber-300">
            config.toml node_id does not match the self node
          </p>
          <p className="mt-1 text-amber-700 dark:text-amber-400">
            Peers written under one id are invisible to the desired state built from the
            other, with no error. The admin API scopes to the self node by default.
          </p>
          <pre className="mt-2 text-xs font-mono text-amber-700 dark:text-amber-400">
            config.toml : {data.config_node_id}{"\n"}
            self node   : {data.self_node_id ?? "—"}
          </pre>
          <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
            Run <code>dn42ctl node adopt-self --dry-run</code> to inspect a fix.
          </p>
        </div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Card label="Node" value={data.node_id} />
        <Card label="Peers" value={`${data.bgp.length} BGP / ${data.ibgp.length} iBGP`} />
        <Card label="WG tunnels" value={String(data.wg.length)} />
      </div>
    </>
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
