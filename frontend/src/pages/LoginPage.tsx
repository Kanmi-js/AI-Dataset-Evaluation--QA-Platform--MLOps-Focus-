import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { ApiError } from "../api/client";

export function LoginPage() {
  const { login, register, user } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to="/" replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "login") await login(email, password);
      else await register(email, password, fullName || undefined);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen w-full flex bg-[#0B0F17] text-slate-100 selection:bg-blue-500 selection:text-white">
      {/* Left Column: Form Section */}
      <div className="flex-1 flex flex-col justify-center px-6 py-12 lg:px-20 xl:px-24">
        <div className="mx-auto w-full max-w-sm">
          {/* Brand Header */}
         <div className="mb-8 flex items-center gap-3">
  <div className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-amber-400 via-amber-500 to-yellow-600 text-black shadow-lg shadow-amber-500/20 ring-1 ring-amber-300/40">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 18l5.5-7.5L12 15l4.5-6.5L22 18" />
      <path d="M6 18l3.5-4.5L12 16.5" opacity="0.6" strokeWidth="1.8" />
      <circle cx="16.5" cy="8.5" r="1.5" fill="currentColor" />
    </svg>
  </div>
  <div>
    <h1 className="text-xl font-extrabold tracking-tight text-white">
      Terrain<span className="bg-gradient-to-r from-amber-400 to-yellow-500 bg-clip-text text-transparent">AI</span>
    </h1>
    <p className="text-xs text-amber-400/80">
      Autonomous Dataset Evaluation & QA
    </p>
  </div>
</div>

          {/* Form Card */}
          <div className="rounded-2xl border border-slate-800/80 bg-slate-900/60 p-8 shadow-2xl backdrop-blur-xl">
            <div className="mb-6">
              <h2 className="text-lg font-semibold text-white">
                {mode === "login" ? "Welcome back" : "Create your account"}
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                {mode === "login"
                  ? "Enter your credentials to access your workspaces."
                  : "Get started with automated dataset evaluation."}
              </p>
            </div>

            <form onSubmit={submit} className="space-y-4">
              {mode === "register" && (
                <Field
                  label="Full name"
                  value={fullName}
                  onChange={setFullName}
                  type="text"
                  placeholder="Oyekanmi"
                  required={false}
                />
              )}
              <Field
                label="Email address"
                value={email}
                onChange={setEmail}
                type="email"
                placeholder="name@example.com"
              />
              <Field
                label="Password"
                value={password}
                onChange={setPassword}
                type="password"
                placeholder="••••••••••••"
              />

              {error && (
                <div className="rounded-lg bg-rose-500/10 border border-rose-500/20 p-3 text-xs text-rose-400">
                  {error}
                </div>
              )}

              <button
                type="submit"
                disabled={busy}
                className="w-full rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 py-2.5 px-4 text-sm font-semibold text-white shadow-lg shadow-blue-500/25 hover:from-blue-500 hover:to-indigo-500 active:scale-[0.99] disabled:opacity-50 transition-all duration-200"
              >
                {busy
                  ? "Processing…"
                  : mode === "login"
                  ? "Sign in to workspace"
                  : "Create account"}
              </button>
            </form>

            <div className="mt-6 text-center">
              <button
                onClick={() => {
                  setMode(mode === "login" ? "register" : "login");
                  setError(null);
                }}
                className="text-xs text-slate-400 hover:text-blue-400 transition-colors"
              >
                {mode === "login" ? (
                  <>
                    Don't have an account?{" "}
                    <span className="font-medium text-blue-400 underline underline-offset-4">
                      Register
                    </span>
                  </>
                ) : (
                  <>
                    Already have an account?{" "}
                    <span className="font-medium text-blue-400 underline underline-offset-4">
                      Sign in
                    </span>
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Right Column: Graphic / Marketing Preview Panel */}
      <div className="hidden lg:flex flex-1 relative bg-gradient-to-br from-slate-900 via-slate-950 to-blue-950/40 items-center justify-center p-12 overflow-hidden border-l border-slate-800/60">
        {/* Background glow effects */}
        <div className="absolute -top-32 -right-32 w-96 h-96 bg-blue-600/10 rounded-full blur-3xl pointer-events-none" />
        <div className="absolute -bottom-32 -left-32 w-96 h-96 bg-indigo-600/10 rounded-full blur-3xl pointer-events-none" />

        <div className="relative z-10 max-w-md space-y-6">
         <div className="inline-flex items-center gap-2 rounded-full border border-amber-500/30 bg-amber-500/10 px-3 py-1 text-xs font-medium text-amber-400">
  <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
  Terrain Engine Active
</div>

<h2 className="text-3xl font-bold tracking-tight text-white leading-snug">
  Navigate Your AI Landscape with <br />
  <span className="bg-gradient-to-r from-amber-400 via-amber-300 to-yellow-500 bg-clip-text text-transparent">
    TerrainAI Ground Truth
  </span>
</h2>
          <p className="text-sm text-slate-400 leading-relaxed">
            Monitor model confidence metrics, execute high-throughput image inpainting masks, and manage annotation validation pipelines with real-time feedback loops.
          </p>

          <div className="grid grid-cols-2 gap-4 pt-4">
            <div className="rounded-xl border border-slate-800/80 bg-slate-900/40 p-4 backdrop-blur-sm">
              <div className="text-2xl font-bold text-white">99.4%</div>
              <div className="text-xs text-slate-400 mt-1">Dataset Accuracy</div>
            </div>
            <div className="rounded-xl border border-slate-800/80 bg-slate-900/40 p-4 backdrop-blur-sm">
              <div className="text-2xl font-bold text-white">&lt;12ms</div>
              <div className="text-xs text-slate-400 mt-1">Inference Latency</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  type,
  placeholder,
  required = true,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type: string;
  placeholder?: string;
  required?: boolean;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-semibold tracking-wider uppercase text-slate-400">
        {label}
      </span>
      <input
        type={type}
        value={value}
        required={required}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-xl border border-slate-800 bg-slate-950/60 px-4 py-2.5 text-sm text-slate-100 placeholder-slate-600 shadow-inner focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 transition-all"
      />
    </label>
  );
}