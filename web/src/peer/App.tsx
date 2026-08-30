import { useEffect, useState } from "react";
import { ThemeToggle } from "../shared/components/ThemeToggle";
import { VersionFooter } from "../shared/components/VersionFooter";
import { AUTOPEER_API } from "../shared/api";
import { StepAuth } from "./steps/StepAuth";
import { StepSubmit } from "./steps/StepSubmit";
import { Success } from "./steps/Success";

export interface SubmitResult {
  proposal_id: number;
  status: string;
  node_id: string;
  node_name: string;
  message: string;
}

export interface Verified {
  asn: number;
  mntner: string;
  session: string;
}

export function App() {
  const [step, setStep] = useState(1);
  const [verified, setVerified] = useState<Verified | null>(null);
  const [result, setResult] = useState<SubmitResult | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(() => hasAuthResponse());

  useEffect(() => {
    const query = new URLSearchParams(location.search);
    const params = query.get("params");
    const signature = query.get("signature");
    if (!params || !signature) return;

    // 兑换后立刻从地址栏抹掉响应:刷新页面会把同一个签名再交一次,而它是一次性的。
    history.replaceState(null, "", location.origin + location.pathname);

    (async () => {
      try {
        const res = await fetch(`${AUTOPEER_API}/session`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ params, signature }),
        });
        const json = await res.json().catch(() => ({ detail: res.statusText }));
        if (!res.ok) throw new Error(json.detail || JSON.stringify(json));
        setVerified({
          asn: json.verified_asn,
          mntner: json.verified_mntner,
          session: json.peer_session_token,
        });
        setStep(2);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setPending(false);
      }
    })();
  }, []);

  const restart = () => {
    setStep(1);
    setVerified(null);
    setResult(null);
    setError("");
  };

  return (
    <div className="min-h-screen flex flex-col items-center px-4 py-8">
      <header className="w-full max-w-2xl flex items-center justify-between mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">dn42 Auto-peer</h1>
        <ThemeToggle />
      </header>

      <StepIndicator current={step} />

      <main className="w-full max-w-2xl">
        {step === 1 && <StepAuth pending={pending} error={error} />}
        {step === 2 && verified && (
          <StepSubmit
            verified={verified}
            onResult={(r) => {
              setResult(r);
              setStep(3);
            }}
          />
        )}
        {step === 3 && result && <Success result={result} onRestart={restart} />}
      </main>

      <footer className="mt-12 text-xs text-zinc-500 text-center space-y-1">
        <p>Peering requests are submitted as proposals and require operator approval.</p>
        <VersionFooter />
      </footer>
    </div>
  );
}

function hasAuthResponse(): boolean {
  const query = new URLSearchParams(location.search);
  return Boolean(query.get("params") && query.get("signature"));
}

function StepIndicator({ current }: { current: number }) {
  return (
    <nav className="w-full max-w-2xl flex items-center justify-center gap-2 mb-8">
      {[1, 2, 3].map((n, i) => (
        <span key={n}>
          {i > 0 && <span className="inline-block w-8 border-t border-zinc-300 dark:border-zinc-700 align-middle" />}
          <span
            className={`inline-flex items-center justify-center w-8 h-8 rounded-full border text-sm font-medium transition-colors duration-200 ${
              n <= current
                ? "bg-black text-white dark:bg-white dark:text-black border-black dark:border-white"
                : "border-zinc-300 dark:border-zinc-700"
            }`}
          >
            {n}
          </span>
        </span>
      ))}
    </nav>
  );
}
