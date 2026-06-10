"use client";

import { useCallback, useRef, useState } from "react";
import ProgressBar from "@/components/ProgressBar";
import { getToken, uploadDocumentWithProgress } from "@/services/api";

// Upload lifecycle for the chat uploader. Mirrors the admin component.
type UploadPhase = "idle" | "uploading" | "indexing";

/**
 * Small, unobtrusive upload control for the chat sidebar.
 *
 * The signed-in user can pick a file and upload it; the current bearer token is
 * attached (the /documents/upload endpoint now requires authentication). Shows
 * the same determinate progress bar + "Indexing…" indeterminate state as the
 * admin uploader, and a short confirmation on success.
 */
export default function ChatUpload() {
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<UploadPhase>("idle");
  const [progress, setProgress] = useState<number | null>(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const busy = phase !== "idle";

  const onUpload = useCallback(async () => {
    if (!file) {
      setError("Please choose a file first.");
      return;
    }
    setPhase("uploading");
    setProgress(0);
    setError(null);
    setNotice(null);
    try {
      await uploadDocumentWithProgress({
        file,
        token: getToken(),
        onProgress: (fraction) => {
          setProgress(fraction);
          if (fraction === null || fraction >= 1) {
            setPhase("indexing");
          }
        },
      });
      setNotice("Document uploaded — it's being indexed and will be searchable shortly.");
      setFile(null);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPhase("idle");
      setProgress(0);
    }
  }, [file]);

  return (
    <div className="chat-upload">
      <span className="chat-upload-title">Add a document</span>

      <input
        ref={fileInputRef}
        type="file"
        aria-label="Choose a document to upload"
        onChange={(e) => {
          setFile(e.target.files?.[0] ?? null);
          setNotice(null);
          setError(null);
        }}
        disabled={busy}
      />

      {file && !busy && <span className="chat-upload-filename">{file.name}</span>}

      <div className="chat-upload-actions">
        <button onClick={() => void onUpload()} disabled={busy || !file}>
          {busy ? "Uploading…" : "Upload"}
        </button>
      </div>

      {phase === "uploading" && (
        <ProgressBar
          value={progress}
          label={
            progress === null ? "Uploading…" : `Uploading… ${Math.round(progress * 100)}%`
          }
        />
      )}
      {phase === "indexing" && <ProgressBar indeterminate label="Indexing…" />}

      {notice && <p className="chat-upload-notice">{notice}</p>}
      {error && <p className="chat-upload-error">{error}</p>}
    </div>
  );
}
