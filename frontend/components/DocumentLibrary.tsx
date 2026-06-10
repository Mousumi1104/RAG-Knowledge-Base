"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ProgressBar from "@/components/ProgressBar";
import {
  AuthError,
  deleteDocument,
  documentFileUrl,
  getToken,
  listDocuments,
  reindexDocument,
  uploadDocumentWithProgress,
  warmUpBackend,
  type DocumentOut,
} from "@/services/api";

// Upload lifecycle: idle (no bar), uploading (determinate bar driven by bytes),
// indexing (indeterminate — the request sits at ~100% while the server indexes).
type UploadPhase = "idle" | "uploading" | "indexing";

interface DocumentLibraryProps {
  /** Called when an authed request returns 401 so the page can show login. */
  onAuthError?: () => void;
}

function statusClass(status: string): string {
  const s = status.toLowerCase();
  if (s === "indexed") return "badge badge-ok";
  if (s === "failed") return "badge badge-error";
  if (s === "processing" || s === "pending") return "badge badge-pending";
  return "badge";
}

/**
 * The signed-in user's personal knowledge base.
 *
 * Lists ONLY the user's own documents (the backend GET /documents is
 * owner-scoped — admin-global docs never appear here), lets them upload new
 * docs, delete their own, reindex, and open a doc inline. Shows the indexing
 * status (pending/processing/indexed/failed) per document.
 */
export default function DocumentLibrary({ onAuthError }: DocumentLibraryProps) {
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [phase, setPhase] = useState<UploadPhase>("idle");
  const [progress, setProgress] = useState<number | null>(0);
  const [docs, setDocs] = useState<DocumentOut[]>([]);
  const [loadingList, setLoadingList] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof AuthError) {
        onAuthError?.();
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    },
    [onAuthError],
  );

  const refresh = useCallback(async () => {
    setLoadingList(true);
    setError(null);
    try {
      const data = await listDocuments();
      setDocs(data.items);
    } catch (e) {
      handleError(e);
    } finally {
      setLoadingList(false);
    }
  }, [handleError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Wake a possibly-cold backend (Render free tier) on mount.
  useEffect(() => {
    void warmUpBackend();
  }, []);

  const onUpload = useCallback(async () => {
    if (!file) {
      setError("Please choose a file first.");
      return;
    }
    setUploading(true);
    setPhase("uploading");
    setProgress(0);
    setError(null);
    setNotice(null);
    try {
      const result = await uploadDocumentWithProgress({
        file,
        token: getToken(),
        onProgress: (fraction) => {
          setProgress(fraction);
          if (fraction === null || fraction >= 1) {
            setPhase("indexing");
          }
        },
      });
      setNotice(
        `Uploaded "${result.filename}" - indexing status: ${result.indexing_status} (${result.chunk_count} chunks).`,
      );
      setFile(null);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      await refresh();
    } catch (e) {
      handleError(e);
    } finally {
      setUploading(false);
      setPhase("idle");
      setProgress(0);
    }
  }, [file, refresh, handleError]);

  const onDelete = useCallback(
    async (id: number) => {
      if (!window.confirm(`Delete document #${id}? This cannot be undone.`)) {
        return;
      }
      setBusyId(id);
      setError(null);
      setNotice(null);
      try {
        await deleteDocument(id);
        setNotice(`Deleted document #${id}.`);
        await refresh();
      } catch (e) {
        handleError(e);
      } finally {
        setBusyId(null);
      }
    },
    [refresh, handleError],
  );

  const onReindex = useCallback(
    async (id: number) => {
      setBusyId(id);
      setError(null);
      setNotice(null);
      try {
        const result = await reindexDocument(id);
        setNotice(`Reindex triggered for #${id}: ${result.indexing_status}.`);
        await refresh();
      } catch (e) {
        handleError(e);
      } finally {
        setBusyId(null);
      }
    },
    [refresh, handleError],
  );

  return (
    <section className="card">
      <div className="chat-header">
        <h2>My documents</h2>
        <div className="chat-toolbar">
          <button className="secondary" onClick={() => void refresh()} disabled={loadingList}>
            {loadingList ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>

      <p className="muted">
        Your personal knowledge base — these documents ground your chat answers. Only you can see
        them.
      </p>

      <div className="row">
        <input
          ref={fileInputRef}
          type="file"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          disabled={uploading}
        />
        <button onClick={() => void onUpload()} disabled={uploading || !file}>
          {uploading ? "Uploading..." : "Upload"}
        </button>
      </div>

      {phase === "uploading" && (
        <ProgressBar
          value={progress}
          label={progress === null ? "Uploading…" : `Uploading… ${Math.round(progress * 100)}%`}
        />
      )}
      {phase === "indexing" && <ProgressBar indeterminate label="Indexing…" />}

      {notice && <p className="notice">{notice}</p>}
      {error && <p className="error">{error}</p>}

      {docs.length === 0 ? (
        <p className="muted">{loadingList ? "Loading documents..." : "No documents yet."}</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Filename</th>
              <th>Type</th>
              <th>Status</th>
              <th>Chunks</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {docs.map((doc) => (
              <tr key={doc.id}>
                <td>{doc.id}</td>
                <td>
                  <a
                    href={documentFileUrl(doc.id)}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="Open document"
                  >
                    {doc.filename}
                  </a>
                </td>
                <td>{doc.document_type}</td>
                <td>
                  <span className={statusClass(doc.indexing_status)}>{doc.indexing_status}</span>
                  {doc.error && (
                    <span className="error-inline" title={doc.error}>
                      {" "}
                      !
                    </span>
                  )}
                </td>
                <td>{doc.chunk_count}</td>
                <td className="actions">
                  <a
                    className="secondary button-link"
                    href={documentFileUrl(doc.id)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Open
                  </a>
                  <button
                    className="secondary"
                    onClick={() => void onReindex(doc.id)}
                    disabled={busyId === doc.id}
                  >
                    Reindex
                  </button>
                  <button
                    className="danger"
                    onClick={() => void onDelete(doc.id)}
                    disabled={busyId === doc.id}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
