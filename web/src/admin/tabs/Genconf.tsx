import { useState } from "react";
import { api } from "../../shared/api";
import { useToast } from "../../shared/components/Toast";

export function Genconf() {
  const [output, setOutput] = useState<string | null>(null);
  const toast = useToast();

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = Object.fromEntries(new FormData(e.currentTarget));
    try {
      const res = await api("/api/genconf", {
        method: "POST",
        body: JSON.stringify({
          overwrite_bird_conf: !!fd.overwrite_bird_conf,
          overwrite_babel_conf: !!fd.overwrite_babel_conf,
        }),
      });
      setOutput(JSON.stringify(res, null, 2));
      toast("genconf done");
    } catch (e) {
      toast((e as Error).message, false);
    }
  };

  return (
    <div className="space-y-4 max-w-md">
      <p className="text-sm text-zinc-500">
        Regenerate Bird / Babel / ROA configuration files.
      </p>
      <form onSubmit={handleSubmit} className="space-y-3">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            name="overwrite_bird_conf"
            defaultChecked
            className="rounded border-zinc-300 dark:border-zinc-700"
          />
          Overwrite bird.conf
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            name="overwrite_babel_conf"
            defaultChecked
            className="rounded border-zinc-300 dark:border-zinc-700"
          />
          Overwrite babel.conf
        </label>
        <button
          type="submit"
          className="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90"
        >
          Run genconf
        </button>
      </form>
      {output !== null && (
        <pre className="text-xs bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded p-3 overflow-x-auto">
          {output}
        </pre>
      )}
    </div>
  );
}
