export interface NodeSummary {
  node_id: string;
  name: string;
}

interface Props {
  nodes: NodeSummary[];
  value: string;
  onChange: (v: string) => void;
  /** Prepend a "— none —" option whose value is the empty string. */
  allowEmpty?: boolean;
  className?: string;
}

export function NodeSelector({ nodes, value, onChange, allowEmpty, className }: Props) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={
        className ??
        "rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-2 py-1 text-sm"
      }
    >
      {allowEmpty && <option value="">— none —</option>}
      {nodes.map((n) => (
        <option key={n.node_id} value={n.node_id}>
          {n.name} ({n.node_id.slice(0, 8)})
        </option>
      ))}
    </select>
  );
}
