"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ProgressBar from "@/components/ProgressBar";
import {
  deleteDocument,
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

function statusClass(status: string): string {
  const s = status.toLowerCase();
  if (s === "indexed") return "badge badge-ok";
  if (s === "failed") return "badge badge-error";
  if (s === "processing" || s === "pending") return "badge badge-pending";
  return "badge";
}

export default function DocumentUpload() {
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

  const refresh = useCallback(async () => {
    setLoadingList(true);
    setError(null);
    try {
      const data = await listDocuments();
      setDocs(data.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Start waking a possibly-cold backend (Render free tier) as soon as the
  // upload UI mounts, so the first upload doesn't hit a cold-start 502.
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
          // Bytes are on the wire; once they're all sent the server indexes
          // synchronously while the promise is still pending — show "Indexing…".
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
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
      setPhase("idle");
      setProgress(0);
    }
  }, [file, refresh]);

  const onDelete = useCallback(
    async (id: number) => {
      setBusyId(id);
      setError(null);
      setNotice(null);
      try {
        await deleteDocument(id);
        setNotice(`Deleted document #${id}.`);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusyId(null);
      }
    },
    [refresh],
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
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusyId(null);
      }
    },
    [refresh],
  );

  return (
    <section className="card">
      <h2>Documents</h2>

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
        <button className="secondary" onClick={() => void refresh()} disabled={loadingList}>
          {loadingList ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {phase === "uploading" && (
        <ProgressBar
          value={progress}
          label={
            progress === null
              ? "Uploading…"
              : `Uploading… ${Math.round(progress * 100)}%`
          }
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
                <td>{doc.filename}</td>
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
