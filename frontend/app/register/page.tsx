"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { login, register } from "@/services/api";
import { useAuth } from "@/components/AuthProvider";

export default function RegisterPage() {
  const router = useRouter();
  const { user, loading, refresh } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Already signed in → go to chat.
  useEffect(() => {
    if (!loading && user) {
      router.replace("/");
    }
  }, [loading, user, router]);

  const onSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (submitting) return;
      setError(null);

      if (password !== confirm) {
        setError("Passwords do not match.");
        return;
      }

      setSubmitting(true);
      try {
        // Register (may 409 duplicate / 403 registration-disabled), then log in.
        await register(email.trim(), password);
        await login(email.trim(), password);
        const me = await refresh();
        if (!me) {
          setError("Account created, but sign-in failed. Try signing in.");
          return;
        }
        router.replace("/");
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg || "Could not create your account.");
      } finally {
        setSubmitting(false);
      }
    },
    [email, password, confirm, submitting, refresh, router],
  );

  return (
    <main className="container">
      <div className="login-wrap">
        <form className="card login-card" onSubmit={(e) => void onSubmit(e)}>
          <h1>Create account</h1>
          <p className="muted">Sign up to chat with your documents.</p>

          <label className="login-label" htmlFor="email">
            Email
          </label>
          <input
            id="email"
            type="text"
            inputMode="email"
            value={email}
            autoComplete="username"
            onChange={(e) => setEmail(e.target.value)}
            disabled={submitting}
          />

          <label className="login-label" htmlFor="password">
            Password
          </label>
          <input
            id="password"
            type="password"
            value={password}
            autoComplete="new-password"
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
          />

          <label className="login-label" htmlFor="confirm">
            Confirm password
          </label>
          <input
            id="confirm"
            type="password"
            value={confirm}
            autoComplete="new-password"
            onChange={(e) => setConfirm(e.target.value)}
            disabled={submitting}
          />

          {error && <p className="error">{error}</p>}

          <button
            type="submit"
            disabled={submitting || !email.trim() || !password || !confirm}
            className="login-submit"
          >
            {submitting ? "Creating account..." : "Create account"}
          </button>

          <p className="login-back">
            Already have an account? <Link href="/login">Sign in</Link>
          </p>
        </form>
      </div>
    </main>
  );
}
