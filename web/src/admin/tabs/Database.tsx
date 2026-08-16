import { useCallback, useEffect, useState } from "react";
import { api, API_PATHS } from "../../shared/api";
import { Table, type Column } from "../../shared/components/Table";

interface TableSummary {
  name: string;
  rows: number;
  redacted: string[];
}

type Row = Record<string, unknown>;

interface TablePage {
  table: string;
  columns: string[];
  rows: Row[];
  total: number;
  limit: number;
  offset: number;
  redacted: string[];
}

const PAGE_SIZE = 100;

function cell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function Database() {
  const [tables, setTables] = useState<TableSummary[]>([]);
  const [selected, setSelected] = useState("");
  const [page, setPage] = useState<TablePage | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");

  useEffect(() => {
    api<TableSummary[]>(API_PATHS.dbTables)
      .then((ts) => {
        setTables(ts);
        setSelected((cur) => cur || ts[0]?.name || "");
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  const load = useCallback(async (table: string, off: number) => {
    if (!table) return;
    try {
      setPage(await api<TablePage>(`${API_PATHS.dbTable(table)}?limit=${PAGE_SIZE}&offset=${off}`));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => { load(selected, offset); }, [selected, offset, load]);

  const columns: Column<Row>[] = (page?.columns ?? []).map((name) => ({
    label: name,
    get: (r) => cell(r[name]),
  }));

  const select = (name: string) => {
    setSelected(name);
    setOffset(0);
  };

  return (
    <div className="flex gap-6">
      <aside className="w-56 shrink-0">
        <h2 className="text-xs uppercase tracking-wider text-zinc-500 mb-2">Tables</h2>
        <ul className="space-y-0.5 text-sm">
          {tables.map((t) => (
            <li key={t.name}>
              <button
                onClick={() => select(t.name)}
                className={`w-full text-left rounded px-2 py-1 ${
                  selected === t.name
                    ? "bg-black text-white dark:bg-white dark:text-black"
                    : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
                }`}
              >
                <span className="font-mono text-xs">{t.name}</span>
                <span className="float-right text-xs opacity-60">{t.rows}</span>
              </button>
            </li>
          ))}
        </ul>
        <p className="mt-4 text-xs text-zinc-500 leading-relaxed">
          Read-only. Writes must go through the entity forms so change
          notifications stay in the same transaction as the write.
        </p>
      </aside>

      <section className="min-w-0 flex-1">
        {error && <p className="text-red-600 dark:text-red-400 text-sm mb-3">{error}</p>}

        {page && (
          <>
            <div className="mb-3 flex items-center justify-between gap-4">
              <div className="text-xs text-zinc-500">
                {page.total} row{page.total === 1 ? "" : "s"}
                {page.redacted.length > 0 && (
                  <span className="ml-3">
                    redacted: <span className="font-mono">{page.redacted.join(", ")}</span>
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2 text-xs">
                <button
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  disabled={offset === 0}
                  className="rounded border border-zinc-300 dark:border-zinc-700 px-2 py-1 disabled:opacity-40"
                >
                  Prev
                </button>
                <span className="text-zinc-500">
                  {page.total === 0 ? 0 : offset + 1}–{Math.min(offset + page.limit, page.total)}
                </span>
                <button
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                  disabled={offset + page.limit >= page.total}
                  className="rounded border border-zinc-300 dark:border-zinc-700 px-2 py-1 disabled:opacity-40"
                >
                  Next
                </button>
              </div>
            </div>

            <div className="overflow-x-auto">
              <Table columns={columns} rows={page.rows} />
            </div>
          </>
        )}
      </section>
    </div>
  );
}
