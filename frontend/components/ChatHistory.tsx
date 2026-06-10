"use client";

import { useCallback, useEffect, useState } from "react";
import {
  deleteConversation,
  getConversations,
  type ConversationSummary,
} from "@/services/api";

interface ChatHistoryProps {
  /** Currently open conversation id, or null for a fresh chat. */
  activeId: number | null;
  /** Bump this number to force a re-fetch of the conversation list. */
  refreshKey: number;
  /** Load a conversation into the main thread. */
  onSelect: (id: number) => void;
  /** Start a fresh chat (clears the thread). */
  onNewChat: () => void;
  /**
   * Called after a conversation is deleted. If the deleted conversation was
   * the active one, the shell should reset the thread to a new chat.
   */
  onDeleted: (id: number) => void;
}

function formatTitle(c: ConversationSummary): string {
  if (c.title && c.title.trim().length > 0) {
    return c.title.trim();
  }
  return "New conversation";
}

export default function ChatHistory({
  activeId,
  refreshKey,
  onSelect,
  onNewChat,
  onDeleted,
}: ChatHistoryProps) {
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const conversations = await getConversations();
      // Newest first: backend ordering is not guaranteed, so sort defensively.
      conversations.sort(
        (a, b) =>
          new Date(b.last_modified).getTime() - new Date(a.last_modified).getTime(),
      );
      setItems(conversations);
    } catch {
      // History is best-effort; leave the list as-is on failure.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const handleDelete = useCallback(
    async (id: number, e: React.MouseEvent) => {
      e.stopPropagation();
      if (deletingId !== null) {
        return;
      }
      setDeletingId(id);
      try {
        await deleteConversation(id);
        setItems((prev) => prev.filter((c) => c.id !== id));
        onDeleted(id);
      } catch {
        // Surface nothing; a stale row is harmless and the next refresh fixes it.
      } finally {
        setDeletingId(null);
      }
    },
    [deletingId, onDeleted],
  );

  return (
    <div className="chat-history">
      <button type="button" className="new-chat-btn" onClick={onNewChat}>
        <span className="new-chat-icon" aria-hidden="true">
          +
        </span>
        New chat
      </button>

      <div className="history-list" role="list">
        {loading && items.length === 0 && (
          <p className="history-empty muted">Loading…</p>
        )}
        {!loading && items.length === 0 && (
          <p className="history-empty muted">No conversations yet.</p>
        )}
        {items.map((c) => {
          const isActive = c.id === activeId;
          return (
            <div
              key={c.id}
              role="listitem"
              className={`history-item${isActive ? " history-item-active" : ""}`}
              onClick={() => onSelect(c.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(c.id);
                }
              }}
              tabIndex={0}
              aria-current={isActive ? "true" : undefined}
            >
              <span className="history-item-title">{formatTitle(c)}</span>
              <button
                type="button"
                className="history-delete"
                aria-label="Delete conversation"
                title="Delete conversation"
                disabled={deletingId === c.id}
                onClick={(e) => void handleDelete(c.id, e)}
              >
                ×
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
