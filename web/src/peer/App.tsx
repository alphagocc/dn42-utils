import { useState } from "react";
import { ThemeToggle } from "../shared/components/ThemeToggle";
import { VersionFooter } from "../shared/components/VersionFooter";
import { Step1Lookup } from "./steps/Step1Lookup";
import { Step2Auth } from "./steps/Step2Auth";
import { Step3Sign } from "./steps/Step3Sign";
import { Step4Submit } from "./steps/Step4Submit";
import { Success } from "./steps/Success";

interface AuthOption {
  index: number;
  scheme: string;
  fingerprint: string | null;
}

export interface Mntner {
  name: string;
  auth_options: AuthOption[];
}

export interface Challenge {
  challenge_id: string;
  nonce: string;
  scheme: string;
  namespace: string;
  mntner: string;
}

export interface SubmitResult {
  proposal_id: number;
  status: string;
  node_id: string;
  message: string;
}

export interface PeerState {
  asn: number;
  mntners: Mntner[];
  challenge: Challenge | null;
  session: string | null;
  result: SubmitResult | null;
}

const INITIAL_STATE: PeerState = {
  asn: 0,
  mntners: [],
  challenge: null,
  session: null,
  result: null,
};

export function App() {
  const [step, setStep] = useState(1);
  const [state, setState] = useState<PeerState>({ ...INITIAL_STATE });

  const restart = () => {
    setStep(1);
    setState({ ...INITIAL_STATE });
  };

  return (
    <div className="min-h-screen flex flex-col items-center px-4 py-8">
      <header className="w-full max-w-2xl flex items-center justify-between mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">dn42 Auto-peer</h1>
        <ThemeToggle />
      </header>

      <StepIndicator current={step} />

      <main className="w-full max-w-2xl">
        {step === 1 && (
          <Step1Lookup
            asn={state.asn}
            onResult={(asn, mntners) => {
              setState((s) => ({ ...s, asn, mntners }));
              setStep(2);
            }}
          />
        )}
        {step === 2 && (
          <Step2Auth
            asn={state.asn}
            mntners={state.mntners}
            onResult={(challenge) => {
              setState((s) => ({ ...s, challenge }));
              setStep(3);
            }}
            onBack={() => setStep(1)}
          />
        )}
        {step === 3 && state.challenge && (
          <Step3Sign
            asn={state.asn}
            challenge={state.challenge}
            onResult={(session) => {
              setState((s) => ({ ...s, session }));
              setStep(4);
            }}
            onBack={() => setStep(2)}
          />
        )}
        {step === 4 && state.session && (
          <Step4Submit
            asn={state.asn}
            challenge={state.challenge!}
            session={state.session}
            onResult={(result) => {
              setState((s) => ({ ...s, result }));
              setStep(5);
            }}
          />
        )}
        {step === 5 && state.result && (
          <Success result={state.result} onRestart={restart} />
        )}
      </main>

      <footer className="mt-12 text-xs text-zinc-500 text-center space-y-1">
        <p>Peering requests are submitted as proposals and require operator approval.</p>
        <VersionFooter />
      </footer>
    </div>
  );
}

function StepIndicator({ current }: { current: number }) {
  return (
    <nav className="w-full max-w-2xl flex items-center justify-center gap-2 mb-8">
      {[1, 2, 3, 4].map((n, i) => (
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
