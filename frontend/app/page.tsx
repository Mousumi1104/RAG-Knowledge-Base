"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import ChatHistory from "@/components/ChatHistory";
import ChatInterface from "@/components/ChatInterface";
import ChatUpload from "@/components/ChatUpload";
import { getConversations } from "@/services/api";

export default function HomePage() {
  // The conversation the thread should display. `null` = a fresh chat.
  const [activeId, setActiveId] = useState<number | null>(null);
  // Bumping this re-fetches the sidebar conversation list.
  const [refreshKey, setRefreshKey] = useState(0);
  // Sidebar visibility on narrow screens.
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const refreshHistory = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  // Load the most recent persisted conversation on mount (global history).
  useEffect(() => {
    let mounted = true;
    void (async () => {
      try {
        const conversations = await getConversations();
        if (!mounted || conversations.length === 0) {
          return;
        }
        conversations.sort(
          (a, b) =>
            new Date(b.last_modified).getTime() - new Date(a.last_modified).getTime(),
        );
        setActiveId(conversations[0].id);
      } catch {
        // Best-effort: a fresh chat is fine if history can't be loaded.
      }
    })();
    return () => {
      mounted = false;
    };
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

        {/* User-facing anonymous upload control. */}
        <div className="sidebar-slot" data-slot="chat-upload">
          <ChatUpload />
        </div>

        <div className="sidebar-footer">
          <Link href="/admin" className="sidebar-admin-link">
            Admin
          </Link>
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
