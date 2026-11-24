import { useEffect, useRef, useState } from "react";
import { api, ApiError, API_PATHS, withNode } from "../../shared/api";

interface FileContents {
  content: string;
  revision: string;
}

export interface ExtraConfDraft {
  text: string;
  baseRevision: string;
}

interface Props {
  nodeId: string;
  drafts: Map<string, ExtraConfDraft>;
}

export function BirdExtraConf({ nodeId, drafts }: Props) {
  const path = withNode(API_PATHS.birdExtraConf, nodeId);
  const [file, setFile] = useState<FileContents | null>(null);
  const [text, setText] = useState(() => drafts.get(nodeId)?.text ?? "");
  const [status, setStatus] = useState<"reading" | "ready" | "saving" | "saved">("reading");
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const saveRequest = useRef<AbortController | null>(null);
  const draft = drafts.get(nodeId);
  const conflict = file && draft && draft.baseRevision !== file.revision;
  const busy = status === "reading" || status === "saving";

  useEffect(() => {
    const controller = new AbortController();
    setStatus("reading");
    setError("");
    api<FileContents>(path, { signal: controller.signal, cache: "no-store" })
      .then((current) => {
        if (controller.signal.aborted) return;
        const pending = drafts.get(nodeId);
        if (pending?.text === current.content) drafts.delete(nodeId);
        setFile(current);
        setText(pending?.text ?? current.content);
        setStatus("ready");
      })
      .catch((e: Error) => {
        if (controller.signal.aborted) return;
        setFile(null);
        setError(e.message);
        setStatus("ready");
      });
    return () => controller.abort();
  }, [drafts, nodeId, path, reloadKey]);

  useEffect(() => () => saveRequest.current?.abort(), []);

  function edit(value: string) {
    if (!file) return;
    if (value === file.content) {
      drafts.delete(nodeId);
    } else {
      drafts.set(nodeId, { text: value, baseRevision: draft?.baseRevision ?? file.revision });
    }
    setText(value);
    setStatus("ready");
  }

  async function save() {
    if (!file || !draft || busy) return;
    const controller = new AbortController();
    saveRequest.current = controller;
    setError("");
    setStatus("saving");
    try {
      const saved = await api<FileContents>(path, {
        method: "PUT",
        body: JSON.stringify({ content: text, expected_revision: file.revision }),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      drafts.delete(nodeId);
      setFile(saved);
      setText(saved.content);
      setStatus("saved");
    } catch (e) {
      if (controller.signal.aborted) return;
      if (e instanceof ApiError && e.status === 409) {
        setFile(null);
        setStatus("reading");
        setReloadKey((key) => key + 1);
      } else {
        setError((e as Error).message);
        setStatus("ready");
      }
    }
  }

  return (
    <section className="max-w-4xl space-y-3">
      <label htmlFor="extra-conf" className="block text-lg font-semibold">BIRD extra.conf</label>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        After saving, check the main BIRD configuration on this node, then run <code>birdc configure</code> to load it.
      </p>
      {conflict && (
        <div className="space-y-2 rounded-md border border-zinc-300 p-3 dark:border-zinc-700">
          <p className="text-sm font-medium">The file changed on the node. Review its current content before saving your edits.</p>
          <pre aria-label="Current file on node" className="max-h-64 overflow-auto whitespace-pre-wrap break-words text-sm">
            {file.content || "(empty file)"}
          </pre>
        </div>
      )}
      {(file || draft) && (
        <textarea
          id="extra-conf"
          value={text}
          onChange={(e) => edit(e.target.value)}
          readOnly={busy || !file}
          spellCheck={false}
          autoCapitalize="off"
          rows={18}
          className="block w-full resize-y rounded-md border border-zinc-300 bg-white p-3 font-mono text-sm dark:border-zinc-700 dark:bg-black"
        />
      )}
      {error && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      <div className="flex flex-wrap items-center gap-3">
        {file && (
          <button type="button" onClick={save} disabled={busy || !draft}
            className="rounded-md bg-black px-3 py-1.5 text-sm font-medium text-white dark:bg-white dark:text-black">
            {status === "saving" ? "Saving…" : conflict ? "Save reviewed edits" : "Save file"}
          </button>
        )}
        {!file && error && (
          <button type="button" onClick={() => setReloadKey((key) => key + 1)} disabled={busy}
            className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm dark:border-zinc-700">
            Retry reading file
          </button>
        )}
        {draft && file && (
          <button type="button" disabled={busy}
            onClick={() => { if (window.confirm("Discard your edits to extra.conf?")) edit(file.content); }}
            className="text-sm underline">Discard edits</button>
        )}
        <span role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
          {status === "reading" ? "Reading file…" : status === "saved" ? "Saved on node." : draft ? "Unsaved changes" : ""}
        </span>
      </div>
    </section>
  );
}
