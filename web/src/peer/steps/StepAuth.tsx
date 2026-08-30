interface Props {
  pending: boolean;
  error: string;
}

const AUTH_URL = "https://dn42.g-load.eu/auth/";

export function StepAuth({ pending, error }: Props) {
  const returnUrl = location.origin + location.pathname;

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-6">
      <h2 className="text-sm font-medium uppercase tracking-wider text-zinc-500 mb-2">
        Step 1 — Prove your ASN
      </h2>
      <p className="text-sm text-zinc-500 mb-4">
        Authentication is handled by the Kioubit dn42 service using the auth methods
        registered on your mntner. You will be sent back here once it succeeds.
      </p>

      {pending ? (
        <p className="text-sm">Verifying your authentication…</p>
      ) : (
        <form action={AUTH_URL} method="get">
          <input type="hidden" name="return" value={returnUrl} />
          <button
            type="submit"
            className="rounded-md bg-black dark:bg-white text-white dark:text-black px-4 py-2 text-sm font-medium hover:opacity-90"
          >
            Authenticate with Kioubit.dn42
          </button>
        </form>
      )}

      {error && <p className="text-xs text-red-600 dark:text-red-400 mt-4">{error}</p>}
    </section>
  );
}
