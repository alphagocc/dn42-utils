import { useCallback, useState } from "react";
import { ThemeToggle } from "../shared/components/ThemeToggle";
import { VersionFooter } from "../shared/components/VersionFooter";
import { NodeSelector } from "../shared/components/NodeSelector";
import { NodeProvider, useNodeScope } from "./NodeContext";
import { Overview } from "./tabs/Overview";
import { Bgp } from "./tabs/Bgp";
import { Ibgp } from "./tabs/Ibgp";
import { Wg } from "./tabs/Wg";
import { Nodes } from "./tabs/Nodes";
import { Proposals } from "./tabs/Proposals";
import { Reports } from "./tabs/Reports";
import { Revisions } from "./tabs/Revisions";
import { Database } from "./tabs/Database";
import { Genconf } from "./tabs/Genconf";

const TAB_NAMES = [
  "overview", "bgp", "ibgp", "wg", "nodes",
  "proposals", "reports", "revisions", "database", "genconf",
] as const;

type TabName = (typeof TAB_NAMES)[number];

const TAB_LABELS: Record<TabName, string> = {
  overview: "Overview",
  bgp: "BGP",
  ibgp: "iBGP",
  wg: "WG",
  nodes: "Nodes",
  proposals: "Proposals",
  reports: "Reports",
  revisions: "Revisions",
  database: "Database",
  genconf: "Genconf",
};

/** Tabs whose data is per-node. The rest are hub-global views. */
const NODE_SCOPED_TABS = new Set<TabName>([
  "overview", "bgp", "ibgp", "wg", "proposals", "reports", "revisions",
]);

interface Props {
  onLogout: () => void;
}

export function Dashboard({ onLogout }: Props) {
  // NodeProvider must wrap the keyed subtree, not live inside it — see NodeContext.
  return (
    <NodeProvider>
      <DashboardInner onLogout={onLogout} />
    </NodeProvider>
  );
}

function DashboardInner({ onLogout }: Props) {
  const [activeTab, setActiveTab] = useState<TabName>("overview");
  const [refreshKey, setRefreshKey] = useState(0);
  const { nodes, nodeId, setNodeId, reload } = useNodeScope();

  const refresh = useCallback(() => {
    reload();
    setRefreshKey((k) => k + 1);
  }, [reload]);

  const tabComponents: Record<TabName, React.ReactNode> = {
    overview: <Overview key={refreshKey} />,
    bgp: <Bgp key={refreshKey} />,
    ibgp: <Ibgp key={refreshKey} />,
    wg: <Wg key={refreshKey} />,
    nodes: <Nodes key={refreshKey} />,
    proposals: <Proposals key={refreshKey} />,
    reports: <Reports key={refreshKey} />,
    revisions: <Revisions key={refreshKey} />,
    database: <Database key={refreshKey} />,
    genconf: <Genconf key={refreshKey} />,
  };

  return (
    <div className="min-h-screen flex flex-col">
      <header className="sticky top-0 z-10 border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-black">
        <div className="mx-auto max-w-7xl flex items-center justify-between px-4 py-3">
          <div className="flex items-center gap-6">
            <h1 className="text-lg font-semibold tracking-tight">dn42ctl</h1>
            <nav className="flex flex-wrap gap-1 text-sm">
              {TAB_NAMES.map((t) => (
                <button
                  key={t}
                  onClick={() => setActiveTab(t)}
                  className={`px-3 py-1.5 rounded-md ${
                    activeTab === t
                      ? "bg-black text-white dark:bg-white dark:text-black"
                      : ""
                  }`}
                >
                  {TAB_LABELS[t]}
                </button>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-2">
            {NODE_SCOPED_TABS.has(activeTab) && nodes.length > 0 && (
              <NodeSelector nodes={nodes} value={nodeId} onChange={setNodeId} />
            )}
            <button
              onClick={refresh}
              className="rounded-md border border-zinc-200 dark:border-zinc-800 px-3 py-1.5 text-xs uppercase tracking-wider hover:bg-zinc-100 dark:hover:bg-zinc-900"
            >
              Refresh
            </button>
            <ThemeToggle />
            <button
              onClick={onLogout}
              className="rounded-md border border-zinc-200 dark:border-zinc-800 px-3 py-1.5 text-xs uppercase tracking-wider hover:bg-zinc-100 dark:hover:bg-zinc-900"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
        {tabComponents[activeTab]}
      </main>
      <footer className="mx-auto w-full max-w-7xl px-4 py-3 text-right">
        <VersionFooter />
      </footer>
    </div>
  );
}
