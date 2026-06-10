"use client";

import { useCallback, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import AdminDashboard from "@/components/AdminDashboard";
import AdminDocuments from "@/components/AdminDocuments";
import UserManagement from "@/components/UserManagement";
import { useAuth } from "@/components/AuthProvider";

export default function AdminPage() {
  const router = useRouter();
  const { user, loading, isAdmin, signOut, refresh } = useAuth();

  // Route guard: send anonymous users to login (returning here afterward).
  useEffect(() => {
    if (!loading && !user) {
      router.replace("/login?next=/admin");
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

  // Authenticated but not an admin → 403 view (no admin UI is rendered).
  if (!isAdmin) {
    return (
      <main className="container">
        <header className="page-header">
          <h1>Access denied</h1>
          <p className="muted">
            Your account ({user.email}) doesn&apos;t have admin access.
          </p>
        </header>
        <nav className="page-nav">
          <Link href="/">Back to chat</Link>
          <button className="secondary" onClick={onLogout}>
            Log out
          </button>
        </nav>
      </main>
    );
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
            <span className="muted api-base">{user.email}</span>
            <Link href="/">Back to chat</Link>
            <button className="secondary" onClick={onLogout}>
              Log out
            </button>
          </nav>
        </div>
      </header>

      <AdminDashboard onAuthError={onAuthError} />
      <UserManagement onAuthError={onAuthError} />
      <AdminDocuments onAuthError={onAuthError} />
    </main>
  );
}
