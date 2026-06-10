"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { clearToken, isAuthed, login } from "@/services/api";
import AdminDashboard from "@/components/AdminDashboard";
import DocumentUpload from "@/components/DocumentUpload";

function LoginForm({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setSubmitting(true);
      setError(null);
      try {
        await login(username, password);
        onSuccess();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setSubmitting(false);
      }
    },
    [username, password, onSuccess],
  );

  return (
    <main className="container">
      <div className="login-wrap">
        <form className="card login-card" onSubmit={(e) => void onSubmit(e)}>
          <h1>Admin Login</h1>
          <p className="muted">Sign in to manage documents and view the dashboard.</p>

          <label className="login-label" htmlFor="username">
            Username
          </label>
          <input
            id="username"
            type="text"
            value={username}
            autoComplete="username"
            onChange={(e) => setUsername(e.target.value)}
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

          <button type="submit" disabled={submitting} className="login-submit">
            {submitting ? "Signing in..." : "Sign in"}
          </button>

          <p className="login-back">
            <Link href="/">Back to chat</Link>
          </p>
        </form>
      </div>
    </main>
  );
}

export default function AdminPage() {
  const [authed, setAuthed] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setAuthed(isAuthed());
    setReady(true);
  }, []);

  const onLogout = useCallback(() => {
    clearToken();
    setAuthed(false);
  }, []);

  // Used by children when a request 401s (expired token).
  const onAuthError = useCallback(() => {
    clearToken();
    setAuthed(false);
  }, []);

  if (!ready) {
    return (
      <main className="container">
        <p className="muted">Loading...</p>
      </main>
    );
  }

  if (!authed) {
    return <LoginForm onSuccess={() => setAuthed(true)} />;
  }

  return (
    <main className="container">
      <header className="page-header">
        <div className="page-header-bar">
          <div>
            <h1>Admin</h1>
            <p className="muted">Document management and indexing dashboard.</p>
          </div>
          <nav className="page-nav">
            <Link href="/">Back to chat</Link>
            <button className="secondary" onClick={onLogout}>
              Logout
            </button>
          </nav>
        </div>
      </header>

      <DocumentUpload />
      <AdminDashboard onAuthError={onAuthError} />
    </main>
  );
}
