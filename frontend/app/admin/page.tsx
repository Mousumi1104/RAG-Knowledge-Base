"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import AdminDashboard from "@/components/AdminDashboard";
import AdminDocuments from "@/components/AdminDocuments";
import UserManagement from "@/components/UserManagement";
import { useAuth } from "@/components/AuthProvider";

/** Admin sidebar sections. Each maps to a view rendered to the right. */
type AdminSection = "overview" | "users" | "documents" | "upload";

const SECTIONS: { id: AdminSection; label: string; icon: string }[] = [
  { id: "overview", label: "Overview", icon: "📊" },
  { id: "users", label: "Users", icon: "👤" },
  { id: "documents", label: "Documents", icon: "📁" },
  { id: "upload", label: "Global Upload", icon: "⬆️" },
];

export default function AdminPage() {
  const router = useRouter();
  const { user, loading, isAdmin, signOut, refresh } = useAuth();
  const [section, setSection] = useState<AdminSection>("overview");
  const [navOpen, setNavOpen] = useState(false);

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

  const selectSection = useCallback((id: AdminSection) => {
    setSection(id);
    setNavOpen(false);
  }, []);

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
    <div className="admin-shell">
      {navOpen && (
        <div className="sidebar-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />
      )}

      <aside className={`admin-sidebar${navOpen ? " admin-sidebar-open" : ""}`}>
        <div className="admin-sidebar-header">
          <span className="sidebar-brand">Admin</span>
          <p className="admin-sidebar-sub">Management console</p>
        </div>

        <nav className="admin-nav">
          {SECTIONS.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`admin-nav-item${section === s.id ? " admin-nav-item-active" : ""}`}
              onClick={() => selectSection(s.id)}
              aria-current={section === s.id ? "page" : undefined}
            >
              <span className="admin-nav-icon" aria-hidden="true">
                {s.icon}
              </span>
              <span>{s.label}</span>
            </button>
          ))}
        </nav>

        <div className="admin-sidebar-footer">
          <span className="sidebar-user-email" title={user.email}>
            {user.email}
          </span>
          <Link href="/" className="sidebar-admin-link">
            ← Back to chat
          </Link>
          <button className="secondary sidebar-logout" onClick={onLogout}>
            Log out
          </button>
        </div>
      </aside>

      <div className="admin-main">
        <div className="admin-topbar">
          <button
            className="sidebar-toggle"
            onClick={() => setNavOpen((v) => !v)}
            aria-label="Toggle navigation"
          >
            ☰
          </button>
          <span className="admin-topbar-title">
            {SECTIONS.find((s) => s.id === section)?.label}
          </span>
        </div>

        <div className="admin-content">
          {section === "overview" && <AdminDashboard onAuthError={onAuthError} />}
          {section === "users" && <UserManagement onAuthError={onAuthError} />}
          {section === "documents" && (
            <AdminDocuments onAuthError={onAuthError} section="list" />
          )}
          {section === "upload" && (
            <AdminDocuments onAuthError={onAuthError} section="upload" />
          )}
        </div>
      </div>
    </div>
  );
}
