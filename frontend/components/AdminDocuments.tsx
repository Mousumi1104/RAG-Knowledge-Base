"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ProgressBar from "@/components/ProgressBar";
import {
  AuthError,
  deleteDocument,
  documentFileUrl,
  listDocuments,
  reindexDocument,
  uploadGlobalDocument,
  warmUpBackend,
  type DocumentOut,
} from "@/services/api";

type UploadPhase = "idle" | "uploading" | "indexing";

interface AdminDocumentsProps {
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

/**
 * Admin document management.
 *
 * For an admin the backend GET /documents returns ALL documents (every user's
 * docs plus admin-global docs). This shows owner / global, lets the admin
 * delete or reindex any document, and exposes a GLOBAL upload control
 * (POST /documents/global) for docs that ground every user's answers.
 */
export default function AdminDocuments({ onAuthError }: AdminDocumentsProps) {
  const [docs, setDocs] = useState<DocumentOut[]>([]);
  const [loadingList, setLoadingList] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Global upload state.
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<UploadPhase>("idle");
  const [progress, setProgress] = useState<number | null>(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploading = phase !== "idle";

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

  useEffect(() => {
    void warmUpBackend();
  }, []);

  const onGlobalUpload = useCallback(async () => {
    if (!file) {
      setError("Please choose a file first.");
      return;
    }
    setPhase("uploading");
    setProgress(0);
    setError(null);
    setNotice(null);
    try {
      const result = await uploadGlobalDocument({
        file,
        onProgress: (fraction) => {
          setProgress(fraction);
          if (fraction === null || fraction >= 1) {
            setPhase("indexing");
          }
        },
      });
      setNotice(
        `Uploaded global "${result.filename}" - indexing status: ${result.indexing_status} (${result.chunk_count} chunks).`,
      );
      setFile(null);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      await refresh();
    } catch (e) {
      handleError(e);
    } finally {
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

  const renderOwner = (doc: DocumentOut): string => {
    if (doc.is_global) return "Global";
    if (doc.owner_id !== null && doc.owner_id !== undefined) {
      return doc.uploaded_by ? `${doc.uploaded_by} (#${doc.owner_id})` : `#${doc.owner_id}`;
    }
    return doc.uploaded_by ?? "—";
  };

  return (
    <section className="card admin-dashboard">
      <div className="chat-header">
        <h2>Document management</h2>
        <div className="chat-toolbar">
          <button className="secondary" onClick={() => void refresh()} disabled={loadingList}>
            {loadingList ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>

      <h3 className="admin-subhead">Global upload</h3>
      <p className="muted">
        A global document grounds every user&apos;s answers but never appears in any user&apos;s
        personal library.
      </p>
      <div className="row">
        <input
          ref={fileInputRef}
          type="file"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          disabled={uploading}
        />
        <button onClick={() => void onGlobalUpload()} disabled={uploading || !file}>
          {uploading ? "Uploading..." : "Upload global"}
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

      <h3 className="admin-subhead">All documents</h3>
      {docs.length === 0 ? (
        <p className="muted">{loadingList ? "Loading documents..." : "No documents."}</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Filename</th>
              <th>Type</th>
              <th>Owner</th>
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
                  {renderOwner(doc)}
                  {doc.is_global && <span className="badge"> global</span>}
                </td>
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
