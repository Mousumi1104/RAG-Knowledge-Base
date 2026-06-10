"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AuthError,
  getAdminHealth,
  getIndexingStatus,
  getStats,
  triggerReindex,
  type AdminHealth,
  type AdminStats,
  type IndexingStatus,
} from "@/services/api";

interface AdminDashboardProps {
  /** Called when an authed request returns 401 so the page can show login. */
  onAuthError: () => void;
}

function statusClass(status: string): string {
  const s = status.toLowerCase();
  if (s === "indexed") return "badge badge-ok";
  if (s === "failed") return "badge badge-error";
  if (s === "processing" || s === "pending") return "badge badge-pending";
  return "badge";
}

/** Render an unknown health value (object or scalar) as readable text. */
function renderHealthValue(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export default function AdminDashboard({ onAuthError }: AdminDashboardProps) {
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [indexing, setIndexing] = useState<IndexingStatus | null>(null);
  const [health, setHealth] = useState<AdminHealth | null>(null);
  const [loading, setLoading] = useState(false);
  const [reindexing, setReindexing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof AuthError) {
        onAuthError();
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    },
    [onAuthError],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, idx, h] = await Promise.all([getStats(), getIndexingStatus(), getAdminHealth()]);
      setStats(s);
      setIndexing(idx);
      setHealth(h);
    } catch (e) {
      handleError(e);
    } finally {
      setLoading(false);
    }
  }, [handleError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onReindexAll = useCallback(async () => {
    setReindexing(true);
    setError(null);
    setNotice(null);
    try {
      const result = await triggerReindex();
      setNotice(result.detail || "Reindex triggered.");
      await refresh();
    } catch (e) {
      handleError(e);
    } finally {
      setReindexing(false);
    }
  }, [handleError, refresh]);

  const items = indexing?.items ?? [];

  return (
    <section className="card admin-dashboard">
      <div className="chat-header">
        <h2>Admin Dashboard</h2>
        <div className="chat-toolbar">
          <button className="secondary" onClick={() => void refresh()} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </button>
          <button onClick={() => void onReindexAll()} disabled={reindexing}>
            {reindexing ? "Reindexing..." : "Reindex all"}
          </button>
        </div>
      </div>

      {notice && <p className="notice">{notice}</p>}
      {error && <p className="error">{error}</p>}

      <div className="stat-grid">
        <div className="stat-card">
          <span className="stat-value">{stats?.total_documents ?? "-"}</span>
          <span className="stat-label">Total documents</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">{stats?.total_chunks ?? "-"}</span>
          <span className="stat-label">Total chunks</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">{stats?.indexed ?? "-"}</span>
          <span className="stat-label">Indexed</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">{stats?.failed ?? "-"}</span>
          <span className="stat-label">Failed</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">{stats?.pending ?? "-"}</span>
          <span className="stat-label">Pending</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">{stats?.processing ?? "-"}</span>
          <span className="stat-label">Processing</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">{stats?.vector_count ?? "-"}</span>
          <span className="stat-label">Vectors</span>
        </div>
        <div className="stat-card">
          <span className="stat-value">
            {stats ? renderHealthValue(stats.vector_db_health?.status) : "-"}
          </span>
          <span className="stat-label">Vector DB health</span>
        </div>
      </div>

      <h3 className="admin-subhead">Health</h3>
      {health ? (
        <table className="health-table">
          <tbody>
            {Object.entries(health).map(([key, value]) => (
              <tr key={key}>
                <th>{key}</th>
                <td>{renderHealthValue(value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">{loading ? "Loading health..." : "No health data."}</p>
      )}

      <h3 className="admin-subhead">Indexing status</h3>
      {items.length === 0 ? (
        <p className="muted">{loading ? "Loading..." : "No documents indexed."}</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Filename</th>
              <th>Status</th>
              <th>Chunks</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.filename}</td>
                <td>
                  <span className={statusClass(item.indexing_status)}>{item.indexing_status}</span>
                  {item.error && (
                    <span className="error-inline" title={item.error}>
                      {" "}
                      !
                    </span>
                  )}
                </td>
                <td>{item.chunk_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
