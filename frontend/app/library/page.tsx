"use client";

import { useCallback, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import DocumentLibrary from "@/components/DocumentLibrary";
import { useAuth } from "@/components/AuthProvider";

export default function LibraryPage() {
  const router = useRouter();
  const { user, loading, signOut, refresh } = useAuth();

  // Route guard: send anonymous users to login (returning here afterward).
  useEffect(() => {
    if (!loading && !user) {
      router.replace("/login?next=/library");
    }
  }, [loading, user, router]);

  const onLogout = useCallback(() => {
    signOut();
    router.replace("/login");
  }, [signOut, router]);

  // A child request 401'd (expired token): re-check the session, which clears
  // it and bounces to login via the guard above.
  const onAuthError = useCallback(() => {
    void refresh();
  }, [refresh]);

  if (loading || !user) {
    return (
      <main className="container">
        <p className="muted">Loading...</p>
      </main>
    );
  }

  return (
    <main className="container">
      <header className="page-header">
        <div className="page-header-bar">
          <div>
            <h1>My documents</h1>
            <p className="muted">Manage your personal knowledge base.</p>
          </div>
          <nav className="page-nav">
            <span className="muted api-base">{user.email}</span>
            <Link href="/">Back to chat</Link>
            <button className="secondary" onClick={onLogout}>
              Log out
            </button>
          </nav>
        </div>
      </header>

      <DocumentLibrary onAuthError={onAuthError} />
    </main>
  );
}
