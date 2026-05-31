import type React from "react";

function esc(v: unknown): string {
  if (v === null || v === undefined) return "—";
  return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export interface Column<T> {
  label: string;
  get: (row: T) => unknown;
  mono?: boolean;
}

interface TableProps<T> {
  columns: Column<T>[];
  rows: T[];
  actions?: (row: T) => React.ReactNode;
}

export function Table<T>({ columns, rows, actions }: TableProps<T>) {
  if (!rows.length) {
    return <p className="text-zinc-500 text-sm py-4">No data.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.label}
                className="text-left text-xs uppercase tracking-wider text-zinc-500 px-3 py-2 font-medium"
              >
                {c.label}
              </th>
            ))}
            {actions && <th />}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-zinc-100 dark:border-zinc-800">
              {columns.map((c) => {
                const val = c.get(row);
                const display =
                  typeof val === "object" && val !== null ? (
                    <code className="text-xs">{JSON.stringify(val)}</code>
                  ) : (
                    esc(val)
                  );
                return (
                  <td key={c.label} className={`px-3 py-2 text-sm${c.mono ? " font-mono" : ""}`}>
                    {display}
                  </td>
                );
              })}
              {actions && (
                <td className="px-3 py-2 text-sm text-right whitespace-nowrap">
                  {actions(row)}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
