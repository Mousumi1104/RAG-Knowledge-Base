"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getConversation, streamChat, type ChatSource } from "@/services/api";
import Sources from "@/components/Sources";

interface ChatTurn {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
}

interface ChatInterfaceProps {
  /**
   * Conversation to display. `null` means a fresh chat (empty thread).
   * Changing this value loads that conversation's messages into the thread.
   * The value is a request token, not a live id — the component tracks the
   * persisted conversation id internally so streaming keeps working.
   */
  loadConversationId: number | null;
  /** Fired when a turn creates/continues a conversation (id from the stream). */
  onConversationChange: (id: number) => void;
  /** Fired after a turn fully completes, so the shell can refresh history. */
  onTurnComplete: () => void;
}

export default function ChatInterface({
  loadConversationId,
  onConversationChange,
  onTurnComplete,
}: ChatInterfaceProps) {
  const [messages, setMessages] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [lastSentQuery, setLastSentQuery] = useState<string | null>(null);

  const bottomRef = useRef<HTMLDivElement | null>(null);

  // --- ChatGPT-style typewriter reveal -------------------------------------
  // Network deltas arrive in bursts (Gemini sends a few large chunks). We buffer
  // them in `pendingRef` and reveal a few characters per tick so the answer
  // appears to "type" smoothly instead of jumping in big blocks.
  const pendingRef = useRef("");
  const finishedRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const appendToLastAssistant = useCallback((text: string) => {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.role === "assistant") {
        next[next.length - 1] = { ...last, content: last.content + text };
      }
      return next;
    });
  }, []);

  const stopTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const ensureTyping = useCallback(() => {
    if (timerRef.current !== null) {
      return;
    }
    timerRef.current = setInterval(() => {
      const pending = pendingRef.current;
      if (pending.length === 0) {
        // Buffer drained: end the turn once the network stream has finished;
        // otherwise idle until the next delta arrives.
        if (finishedRef.current) {
          stopTimer();
          setStreaming(false);
        }
        return;
      }
      // Reveal faster when the backlog is large so we never lag far behind,
      // but always a couple of chars per tick for a smooth, readable cadence.
      const n = Math.max(2, Math.ceil(pending.length / 14));
      pendingRef.current = pending.slice(n);
      appendToLastAssistant(pending.slice(0, n));
    }, 18);
  }, [appendToLastAssistant, stopTimer]);

  // Stop the typewriter timer on unmount.
  useEffect(() => () => stopTimer(), [stopTimer]);

  // Load the requested conversation into the thread whenever the shell asks
  // for a different one. `null` resets to a fresh chat. We never reload while a
  // turn is streaming so we don't clobber in-flight content.
  useEffect(() => {
    if (loadConversationId === null) {
      // Fresh chat.
      setMessages([]);
      setConversationId(null);
      setError(null);
      setLastSentQuery(null);
      return;
    }
    if (loadConversationId === conversationId) {
      // Already showing this conversation (e.g. just created by streaming).
      return;
    }
    let mounted = true;
    void (async () => {
      try {
        const detail = await getConversation(loadConversationId);
        if (!mounted) {
          return;
        }
        setConversationId(detail.id);
        setError(null);
        setLastSentQuery(null);
        setMessages(
          detail.messages.map((m) => ({
            role: m.role,
            content: m.content,
            sources: m.sources ?? undefined,
          })),
        );
      } catch {
        // Loading is best-effort; leave the current thread untouched on failure.
      }
    })();
    return () => {
      mounted = false;
    };
    // `conversationId` intentionally excluded: this effect reacts to the shell's
    // load request, not to internal id changes made by streaming.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadConversationId]);

  // Auto-scroll to the newest content.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, streaming]);

  const send = useCallback(
    async (query: string) => {
      const trimmed = query.trim();
      if (!trimmed || streaming) {
        return;
      }

      setError(null);
      setStreaming(true);
      setLastSentQuery(trimmed);

      // Reset the typewriter buffer for this new turn.
      pendingRef.current = "";
      finishedRef.current = false;

      // Append the user turn and an empty assistant placeholder.
      setMessages((prev) => [
        ...prev,
        { role: "user", content: trimmed },
        { role: "assistant", content: "" },
      ]);

      await streamChat({
        query: trimmed,
        conversationId: conversationId ?? undefined,
        onDelta: (text) => {
          // Buffer the chunk; the typewriter loop reveals it gradually.
          pendingRef.current += text;
          ensureTyping();
        },
        onDone: (payload) => {
          // Attach sources now; the typewriter keeps revealing any buffered
          // text and flips `streaming` off once the buffer is fully drained.
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last && last.role === "assistant") {
              next[next.length - 1] = { ...last, sources: payload.sources };
            }
            return next;
          });
          setConversationId(payload.conversation_id);
          onConversationChange(payload.conversation_id);
          finishedRef.current = true;
          ensureTyping();
          // Let the shell refresh the sidebar so a brand-new chat appears.
          onTurnComplete();
        },
        onError: (message) => {
          stopTimer();
          pendingRef.current = "";
          finishedRef.current = true;
          setError(message);
          setStreaming(false);
          // Drop the empty assistant placeholder so the failed turn is clean.
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last && last.role === "assistant" && last.content === "") {
              next.pop();
            }
            return next;
          });
        },
      });
    },
    [conversationId, streaming, ensureTyping, stopTimer, onConversationChange, onTurnComplete],
  );

  const onSubmit = useCallback(() => {
    const value = input;
    setInput("");
    void send(value);
  }, [input, send]);

  const onRetry = useCallback(() => {
    if (lastSentQuery) {
      void send(lastSentQuery);
    }
  }, [lastSentQuery, send]);

  return (
    <section className="chat-pane">
      <div
        className="chat-log"
        role="log"
        aria-live="polite"
        aria-busy={streaming}
      >
        {messages.length === 0 && (
          <div className="chat-empty">
            <h2>How can I help?</h2>
            <p className="muted">Ask a question about your documents.</p>
          </div>
        )}
        <div className="chat-thread">
          {messages.map((turn, i) => {
            const isLast = i === messages.length - 1;
            const showTyping =
              streaming && isLast && turn.role === "assistant" && turn.content === "";
            return (
              <div key={i} className={`bubble-row bubble-row-${turn.role}`}>
                <div className={`bubble bubble-${turn.role}`}>
                  <div className="bubble-role">
                    {turn.role === "user" ? "You" : "Assistant"}
                  </div>
                  {showTyping ? (
                    <div className="typing-indicator" aria-label="Assistant is typing">
                      <span />
                      <span />
                      <span />
                    </div>
                  ) : (
                    <div className="bubble-content">
                      {turn.content}
                      {streaming && isLast && turn.role === "assistant" && (
                        <span className="stream-cursor" aria-hidden="true" />
                      )}
                    </div>
                  )}
                  {turn.role === "assistant" && turn.sources && turn.sources.length > 0 && (
                    <Sources sources={turn.sources} />
                  )}
                </div>
              </div>
            );
          })}
        </div>
        <div ref={bottomRef} />
      </div>

      <div className="composer">
        {error && (
          <div className="chat-error" role="alert">
            <span className="error-inline">{error}</span>
            {lastSentQuery && (
              <button type="button" className="secondary" onClick={onRetry} disabled={streaming}>
                Retry
              </button>
            )}
          </div>
        )}

        <div className="composer-inner">
          <textarea
            className="chat-textarea"
            value={input}
            placeholder="Message RAG Chatbot…"
            rows={1}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (!streaming && input.trim()) {
                  onSubmit();
                }
              }
            }}
            disabled={streaming}
            aria-label="Chat message"
          />
          <button
            type="button"
            className="composer-send"
            onClick={onSubmit}
            disabled={streaming || !input.trim()}
            aria-label="Send message"
          >
            {streaming ? "…" : "↑"}
          </button>
        </div>
        <p className="composer-hint muted">
          Enter to send · Shift+Enter for a new line
        </p>
      </div>
    </section>
  );
}
