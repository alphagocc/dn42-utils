import { type FormEvent, type ReactNode, useEffect } from "react";
import { createPortal } from "react-dom";

interface ModalProps {
  onClose: () => void;
  children: ReactNode;
}

export function Modal({ onClose, children }: ModalProps) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 px-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-lg rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-black p-6">
        {children}
      </div>
    </div>,
    document.body,
  );
}

export interface FieldDef {
  name: string;
  label: string;
  type?: "text" | "number" | "password" | "select" | "checkbox";
  required?: boolean;
  value?: string | number | boolean;
  options?: { value: string; label: string; selected?: boolean }[];
}

interface FormModalProps {
  title: string;
  fields: FieldDef[];
  onSubmit: (data: Record<string, string>) => Promise<void>;
  onClose: () => void;
}

export function FormModal({ title, fields, onSubmit, onClose }: FormModalProps) {
  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.currentTarget));
    await onSubmit(data as Record<string, string>);
  };

  return (
    <Modal onClose={onClose}>
      <h3 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-4">{title}</h3>
      <form onSubmit={handleSubmit} className="space-y-3">
        {fields.map((f) => (
          <label key={f.name} className="block">
            <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
              {f.label}
              {f.required ? " *" : ""}
            </span>
            {f.type === "select" ? (
              <select
                name={f.name}
                required={f.required}
                defaultValue={f.options?.find((o) => o.selected)?.value ?? f.options?.[0]?.value}
                className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm"
              >
                {f.options?.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : f.type === "checkbox" ? (
              <input
                type="checkbox"
                name={f.name}
                defaultChecked={!!f.value}
                className="rounded border-zinc-300 dark:border-zinc-700"
              />
            ) : (
              <input
                type={f.type || "text"}
                name={f.name}
                defaultValue={f.value != null ? String(f.value) : ""}
                required={f.required}
                className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm"
              />
            )}
          </label>
        ))}
        <div className="flex gap-2 pt-2">
          <button
            type="submit"
            className="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90"
          >
            Save
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm"
          >
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}

interface ConfirmModalProps {
  message: string;
  onConfirm: () => Promise<void>;
  onClose: () => void;
}

export function ConfirmModal({ message, onConfirm, onClose }: ConfirmModalProps) {
  return (
    <Modal onClose={onClose}>
      <p className="text-sm mb-4">{message}</p>
      <div className="flex gap-2">
        <button
          onClick={async () => {
            await onConfirm();
          }}
          className="rounded-md bg-red-600 text-white px-4 py-2 text-sm font-medium hover:opacity-90"
        >
          Confirm
        </button>
        <button
          onClick={onClose}
          className="rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm"
        >
          Cancel
        </button>
      </div>
    </Modal>
  );
}
