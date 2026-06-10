"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { login } from "@/services/api";
import { useAuth } from "@/components/AuthProvider";

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, loading, refresh } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // `next` lets us return the user to where they were headed; defaults to chat.
  const next = searchParams.get("next") || "/";

  // Already signed in → bounce to the destination.
  useEffect(() => {
    if (!loading && user) {
      router.replace(next);
    }
  }, [loading, user, next, router]);

  const onSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (submitting) return;
      setSubmitting(true);
      setError(null);
      try {
        await login(email.trim(), password);
        const me = await refresh();
        if (!me) {
          setError("Signed in, but could not load your account. Please try again.");
          return;
        }
        router.replace(next);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        // The backend returns 401 with a detail for bad credentials.
        setError(msg || "Invalid email or password.");
      } finally {
        setSubmitting(false);
      }
    },
    [email, password, submitting, refresh, router, next],
  );

  return (
    <main className="container">
      <div className="login-wrap">
        <form className="card login-card" onSubmit={(e) => void onSubmit(e)}>
          <h1>Sign in</h1>
          <p className="muted">Sign in to chat with your documents.</p>

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
            autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
          />

          {error && <p className="error">{error}</p>}

          <button
            type="submit"
            disabled={submitting || !email.trim() || !password}
            className="login-submit"
          >
            {submitting ? "Signing in..." : "Sign in"}
          </button>

          <p className="login-back">
            Don&apos;t have an account? <Link href="/register">Create one</Link>
          </p>
        </form>
      </div>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <main className="container">
          <p className="muted">Loading...</p>
        </main>
      }
    >
      <LoginForm />
    </Suspense>
  );
}
