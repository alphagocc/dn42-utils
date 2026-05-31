import { toggleTheme } from "../theme";

export function ThemeToggle() {
  return (
    <button
      type="button"
      onClick={toggleTheme}
      className="rounded-md border border-zinc-200 dark:border-zinc-800 px-3 py-1.5 text-xs uppercase tracking-wider hover:bg-zinc-100 dark:hover:bg-zinc-900"
    >
      Theme
    </button>
  );
}
