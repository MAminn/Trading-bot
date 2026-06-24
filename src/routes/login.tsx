import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowRight, Mail } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { LogoMark } from "@/components/Logo";
import { supabase } from "@/integrations/supabase/client";
import { lovable } from "@/integrations/lovable";

export const Route = createFileRoute("/login")({
  head: () => ({
    meta: [
      { title: "Log in — Helix" },
      { name: "description", content: "Secure access to your Helix trading dashboard." },
    ],
  }),
  component: Login,
});

function Login() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  // If already signed in, send straight into the app.
  useEffect(() => {
    supabase.auth.getUser().then(({ data }) => {
      if (data.user) navigate({ to: "/app/dashboard" });
    });
  }, [navigate]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      if (mode === "signup") {
        const { error } = await supabase.auth.signUp({
          email,
          password,
          options: { emailRedirectTo: `${window.location.origin}/app/dashboard` },
        });
        if (error) throw error;
        toast.success("Account created. You're in.");
      } else {
        const { error } = await supabase.auth.signInWithPassword({ email, password });
        if (error) throw error;
        toast.success("Welcome back.");
      }
      navigate({ to: "/app/dashboard" });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Authentication failed");
    } finally {
      setLoading(false);
    }
  }

  async function handleGoogle() {
    setLoading(true);
    const result = await lovable.auth.signInWithOAuth("google", {
      redirect_uri: window.location.origin,
    });
    if (result.error) {
      toast.error(result.error.message || "Google sign-in failed");
      setLoading(false);
      return;
    }
    if (result.redirected) return;
    navigate({ to: "/app/dashboard" });
  }

  return (
    <div className="relative grid min-h-screen lg:grid-cols-2">
      <div className="pointer-events-none absolute inset-0 bg-aurora opacity-80" aria-hidden />
      <div className="pointer-events-none absolute inset-0 bg-grid opacity-50" aria-hidden />

      {/* Left: form */}
      <div className="relative z-10 flex flex-col px-8 py-10 md:px-16">
        <Link to="/"><LogoMark /></Link>
        <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center">
          <h1 className="font-display text-4xl font-semibold tracking-tight">
            {mode === "signin" ? "Welcome back." : "Create your account."}
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {mode === "signin"
              ? "Log in to your trading cockpit. Your Binance keys never leave the vault."
              : "Spin up a Helix account and connect your exchange in minutes."}
          </p>

          <form className="mt-8 space-y-4" onSubmit={handleSubmit}>
            <Field
              label="Email"
              type="email"
              placeholder="alex@helix.trade"
              value={email}
              onChange={setEmail}
            />
            <Field
              label="Password"
              type="password"
              placeholder="••••••••••"
              value={password}
              onChange={setPassword}
            />

            <button
              type="submit"
              disabled={loading}
              className="group inline-flex w-full items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-4 py-2.5 font-semibold text-primary-foreground shadow-[0_10px_30px_-10px_oklch(0.85_0.18_165/70%)] transition-transform hover:scale-[1.01] disabled:opacity-60"
            >
              {loading ? "Working…" : mode === "signin" ? "Enter cockpit" : "Create account"}
              <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
            </button>
          </form>

          <div className="my-6 flex items-center gap-3 text-xs text-muted-foreground">
            <div className="h-px flex-1 bg-border" /> or <div className="h-px flex-1 bg-border" />
          </div>

          <div className="grid grid-cols-1 gap-3">
            <button
              type="button"
              onClick={handleGoogle}
              disabled={loading}
              className="inline-flex items-center justify-center gap-2 rounded-lg border border-border bg-card/40 px-3 py-2 text-sm hover:bg-card/70 disabled:opacity-60"
            >
              <Mail className="h-4 w-4" /> Continue with Google
            </button>
          </div>

          <p className="mt-8 text-center text-sm text-muted-foreground">
            {mode === "signin" ? (
              <>
                No account yet?{" "}
                <button onClick={() => setMode("signup")} className="text-primary hover:underline">
                  Create one
                </button>
              </>
            ) : (
              <>
                Already have an account?{" "}
                <button onClick={() => setMode("signin")} className="text-primary hover:underline">
                  Sign in
                </button>
              </>
            )}
          </p>
        </div>
      </div>

      {/* Right: showcase */}
      <div className="relative hidden overflow-hidden border-l border-border bg-sidebar lg:block">
        <div className="absolute inset-0 bg-aurora opacity-80" aria-hidden />
        <div className="absolute inset-0 bg-grid opacity-60" aria-hidden />
        <div className="relative flex h-full flex-col items-center justify-center gap-6 p-12">
          <div className="card-elevated w-full max-w-md p-6">
            <div className="text-xs uppercase tracking-widest text-muted-foreground">Live engine</div>
            <div className="mt-1 font-display text-3xl font-semibold">ETHUSDT · v4.2</div>
            <div className="mt-6 grid grid-cols-3 gap-3">
              {[
                { k: "64.2%", v: "Win" },
                { k: "2.18", v: "PF" },
                { k: "1.94", v: "Sharpe" },
              ].map((s) => (
                <div key={s.v} className="rounded-lg border border-border bg-card/60 p-3">
                  <div className="font-mono text-xl font-semibold">{s.k}</div>
                  <div className="text-xs text-muted-foreground">{s.v}</div>
                </div>
              ))}
            </div>
            <div className="mt-6 rounded-lg border border-success/30 bg-success/10 p-3 text-sm text-success">
              ▲ Signal · BUY · conf 0.82
            </div>
          </div>
          <p className="max-w-md text-center text-sm text-muted-foreground">
            "It's the first dashboard that feels like it was built by traders, not by a backend team."
          </p>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  type,
  placeholder,
  value,
  onChange,
}: {
  label: string;
  type: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-muted-foreground">{label}</span>
      <input
        type={type}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required
        autoComplete={type === "password" ? "current-password" : "email"}
        className="w-full rounded-lg border border-border bg-input/40 px-3 py-2.5 text-sm placeholder:text-muted-foreground/60 focus:border-primary/60 focus:outline-none focus:ring-2 focus:ring-primary/20"
      />
    </label>
  );
}
