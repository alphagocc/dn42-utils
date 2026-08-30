import { useCallback, useEffect, useState } from "react";
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

const HEADER_BUTTON =
  "rounded-md border border-zinc-200 dark:border-zinc-800 px-3 py-1.5 text-xs uppercase tracking-wider whitespace-nowrap hover:bg-zinc-100 dark:hover:bg-zinc-900";

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

function TabNav({
  active,
  onSelect,
}: {
  active: TabName;
  onSelect: (tab: TabName) => void;
}) {
  return (
    <nav className="flex flex-col gap-1 text-sm">
      {TAB_NAMES.map((t) => (
        <button
          key={t}
          onClick={() => onSelect(t)}
          aria-current={active === t ? "page" : undefined}
          className={`rounded-md px-3 py-2 text-left ${
            active === t
              ? "bg-black text-white dark:bg-white dark:text-black"
              : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
          }`}
        >
          {TAB_LABELS[t]}
        </button>
      ))}
    </nav>
  );
}

function DashboardInner({ onLogout }: Props) {
  const [activeTab, setActiveTab] = useState<TabName>("overview");
  const [refreshKey, setRefreshKey] = useState(0);
  const [navOpen, setNavOpen] = useState(false);
  const { nodes, nodeId, setNodeId, reload } = useNodeScope();

  const refresh = useCallback(() => {
    reload();
    setRefreshKey((k) => k + 1);
  }, [reload]);

  const selectTab = useCallback((tab: TabName) => {
    setActiveTab(tab);
    setNavOpen(false);
  }, []);

  useEffect(() => {
    if (!navOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [navOpen]);

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
      <header className="sticky top-0 z-30 border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-black">
        <div className="mx-auto max-w-7xl flex h-14 items-center justify-between gap-3 px-4">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setNavOpen(true)}
              aria-label="Open navigation"
              className={`lg:hidden ${HEADER_BUTTON}`}
            >
              Menu
            </button>
            <h1 className="text-lg font-semibold tracking-tight">dn42ctl</h1>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {NODE_SCOPED_TABS.has(activeTab) && nodes.length > 0 && (
              <NodeSelector nodes={nodes} value={nodeId} onChange={setNodeId} />
            )}
            <button onClick={refresh} className={HEADER_BUTTON}>
              Refresh
            </button>
            <ThemeToggle />
            <button onClick={onLogout} className={HEADER_BUTTON}>
              Sign out
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto flex w-full max-w-7xl flex-1">
        <aside className="hidden w-52 shrink-0 border-r border-zinc-200 dark:border-zinc-800 lg:block">
          <div className="sticky top-14 p-3">
            <TabNav active={activeTab} onSelect={selectTab} />
          </div>
        </aside>
        <main className="min-w-0 flex-1 px-4 py-6">{tabComponents[activeTab]}</main>
      </div>

      {navOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={() => setNavOpen(false)}
          />
          <aside className="absolute inset-y-0 left-0 w-60 overflow-y-auto border-r border-zinc-200 dark:border-zinc-800 bg-white dark:bg-black p-3">
            <TabNav active={activeTab} onSelect={selectTab} />
          </aside>
        </div>
      )}

      <footer className="mx-auto w-full max-w-7xl px-4 py-3 text-right">
        <VersionFooter />
      </footer>
    </div>
  );
}
