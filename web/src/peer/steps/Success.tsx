import type { SubmitResult } from "../App";

interface Props {
  result: SubmitResult;
  onRestart: () => void;
}

export function Success({ result, onRestart }: Props) {
  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6 text-center">
      <h2 className="text-lg font-semibold mb-2">Peering request submitted</h2>
      <p className="text-sm text-zinc-500 mb-4">{result.message}</p>
      <div className="inline-block text-left bg-white dark:bg-black border border-zinc-200 dark:border-zinc-800 rounded-lg p-4 text-sm space-y-1">
        <p>
          <span className="text-zinc-500">Proposal ID:</span>{" "}
          <strong>#{result.proposal_id}</strong>
        </p>
        <p>
          <span className="text-zinc-500">Status:</span> {result.status}
        </p>
        <p>
          <span className="text-zinc-500">Node ID:</span>{" "}
          <code className="text-xs">{result.node_id}</code>
        </p>
      </div>
      <p className="mt-6 text-xs text-zinc-500">
        The operator will review your request. You can close this page.
      </p>
      <button onClick={onRestart} className="mt-4 text-xs underline text-zinc-500">
        Submit another request
      </button>
    </section>
  );
}
