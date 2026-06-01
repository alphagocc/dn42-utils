import { useState } from "react";
import { api, API_PATHS } from "../shared/api";
import { ThemeToggle } from "../shared/components/ThemeToggle";

interface Props {
  onLogin: (token: string) => void;
}

export function Login({ onLogin }: Props) {
  const [error, setError] = useState("");

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const t = new FormData(e.currentTarget).get("token") as string;
    if (!t.trim()) return;
    try {
      await api(`${API_PATHS.showAll}?live=false`, {}, t.trim());
      sessionStorage.setItem("dn42ctl_admin_token", t.trim());
      onLogin(t.trim());
    } catch {
      setError("Invalid token or server unreachable.");
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <main className="w-full max-w-sm">
        <header className="mb-8 flex items-center justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">dn42ctl</h1>
          <ThemeToggle />
        </header>
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
          <h2 className="text-sm font-medium uppercase tracking-wider text-zinc-500">
            Admin sign in
          </h2>
          <p className="mt-2 text-sm text-zinc-500">
            Paste the admin token from <code>/etc/dn42ctl/server.env</code>.
          </p>
          <form onSubmit={handleSubmit} className="mt-5 space-y-4">
            <label className="block">
              <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
                Token
              </span>
              <input
                name="token"
                type="password"
                autoComplete="off"
                required
                className="block w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-black px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100"
              />
            </label>
            <button
              type="submit"
              className="w-full rounded-md bg-black dark:bg-white text-white dark:text-black px-3 py-2 text-sm font-medium hover:opacity-90"
            >
              Sign in
            </button>
            {error && (
              <p className="text-xs text-red-600 dark:text-red-400">{error}</p>
            )}
          </form>
        </section>
        <p className="mt-8 text-xs text-zinc-500 text-center">
          React SPA · served by nginx · talks to <code>/api/</code>
        </p>
      </main>
    </div>
  );
}
