"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import ChatHistory from "@/components/ChatHistory";
import ChatInterface from "@/components/ChatInterface";
import ChatUpload from "@/components/ChatUpload";
import { useAuth } from "@/components/AuthProvider";

export default function HomePage() {
  const router = useRouter();
  const { user, loading, isAdmin, signOut } = useAuth();

  // The conversation the thread should display. `null` = a fresh chat.
  // Per-session each login starts a fresh chat (we no longer auto-load the
  // most recent conversation); history remains available via the sidebar.
  const [activeId, setActiveId] = useState<number | null>(null);
  // Bumping this re-fetches the sidebar conversation list.
  const [refreshKey, setRefreshKey] = useState(0);
  // Sidebar visibility on narrow screens.
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Route guard: unauthenticated users go to the login page.
  useEffect(() => {
    if (!loading && !user) {
      router.replace("/login?next=/");
    }
  }, [loading, user, router]);

  const refreshHistory = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  const handleSelect = useCallback((id: number) => {
    setActiveId(id);
    setSidebarOpen(false);
  }, []);

  const handleNewChat = useCallback(() => {
    setActiveId(null);
    setSidebarOpen(false);
  }, []);

  // A turn created or continued a conversation: track it as active so selecting
  // it in the sidebar (after refresh) highlights the right row.
  const handleConversationChange = useCallback((id: number) => {
    setActiveId(id);
  }, []);

  const handleDeleted = useCallback(
    (id: number) => {
      // If the active conversation was deleted, drop back to a fresh chat.
      setActiveId((current) => (current === id ? null : current));
      refreshHistory();
    },
    [refreshHistory],
  );

  const onLogout = useCallback(() => {
    signOut();
    router.replace("/login");
  }, [signOut, router]);

  // While the session resolves (or before redirecting an anon user), avoid
  // flashing the authenticated UI.
  if (loading || !user) {
    return (
      <main className="container">
        <p className="muted">Loading...</p>
      </main>
    );
  }

  return (
    <div className="chat-shell">
      {sidebarOpen && (
        <div
          className="sidebar-scrim"
          aria-hidden="true"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside className={`chat-sidebar${sidebarOpen ? " chat-sidebar-open" : ""}`}>
        <div className="sidebar-header">
          <span className="sidebar-brand">RAG Chatbot</span>
        </div>

        <ChatHistory
          activeId={activeId}
          refreshKey={refreshKey}
          onSelect={handleSelect}
          onNewChat={handleNewChat}
          onDeleted={handleDeleted}
        />

        {/* User-facing upload control. */}
        <div className="sidebar-slot" data-slot="chat-upload">
          <ChatUpload />
        </div>

        <div className="sidebar-footer">
          <div className="sidebar-user" title={user.email}>
            <span className="sidebar-user-email">{user.email}</span>
          </div>
          <Link href="/library" className="sidebar-admin-link">
            My documents
          </Link>
          {isAdmin && (
            <Link href="/admin" className="sidebar-admin-link">
              Admin
            </Link>
          )}
          <button type="button" className="secondary sidebar-logout" onClick={onLogout}>
            Log out
          </button>
        </div>
      </aside>

      <main className="chat-main">
        <header className="chat-topbar">
          <button
            type="button"
            className="sidebar-toggle"
            aria-label="Toggle conversation history"
            aria-expanded={sidebarOpen}
            onClick={() => setSidebarOpen((o) => !o)}
          >
            ☰
          </button>
          <span className="chat-topbar-title">RAG Chatbot</span>
        </header>

        <ChatInterface
          loadConversationId={activeId}
          onConversationChange={handleConversationChange}
          onTurnComplete={refreshHistory}
        />
      </main>
    </div>
  );
}
