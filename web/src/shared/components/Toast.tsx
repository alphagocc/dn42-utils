import { createContext, useCallback, useContext, useRef, useState } from "react";

interface ToastState {
  message: string;
  ok: boolean;
  id: number;
}

const ToastContext = createContext<(msg: string, ok?: boolean) => void>(() => {});

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toast, setToast] = useState<ToastState | null>(null);
  const timerRef = useRef(0);

  const show = useCallback((message: string, ok = true) => {
    clearTimeout(timerRef.current);
    const id = Date.now();
    setToast({ message, ok, id });
    timerRef.current = window.setTimeout(() => setToast(null), 3500);
  }, []);

  return (
    <ToastContext value={show}>
      {children}
      {toast && (
        <div
          className={`fixed top-4 right-4 z-50 max-w-sm rounded-md border px-4 py-3 text-sm shadow-lg ${
            toast.ok
              ? "border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black"
              : "border-red-400 dark:border-red-600 bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300"
          }`}
        >
          {toast.message}
        </div>
      )}
    </ToastContext>
  );
}
