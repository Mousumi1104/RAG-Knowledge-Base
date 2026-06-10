"use client";

/**
 * A small accessible progress bar.
 *
 * Pass a `value` in [0, 1] for a determinate bar (renders the corresponding
 * `aria-valuenow` 0–100). Pass `indeterminate` (or omit `value`) to show an
 * animated sweep with no numeric value — used while the server indexes a freshly
 * uploaded document or when upload progress is not length-computable.
 */
export default function ProgressBar({
  value,
  indeterminate,
  label,
}: {
  value?: number | null;
  indeterminate?: boolean;
  label?: string;
}) {
  const isIndeterminate = indeterminate || value === null || value === undefined;
  const pct = isIndeterminate ? 0 : Math.round(Math.min(1, Math.max(0, value)) * 100);

  return (
    <div className="upload-status">
      {label && <span className="upload-status-label">{label}</span>}
      <div
        className={`progress${isIndeterminate ? " progress-indeterminate" : ""}`}
        role="progressbar"
        aria-label={label ?? "Upload progress"}
        aria-valuemin={0}
        aria-valuemax={100}
        {...(isIndeterminate ? {} : { "aria-valuenow": pct })}
      >
        <div className="progress-bar" style={isIndeterminate ? undefined : { width: `${pct}%` }} />
      </div>
    </div>
  );
}
