import { documentFileUrl, type ChatSource } from "@/services/api";

/**
 * Horizontally scrollable strip of cited sources.
 *
 * Sources are rendered in the order received (the backend already orders them
 * by relevance) with no numeric score. Each chip links to the inline document
 * file at the cited page. Renders nothing when there are no sources (e.g. an
 * answer grounded only in admin-global documents returns an empty array).
 */
export default function Sources({ sources }: { sources: ChatSource[] }) {
  if (sources.length === 0) {
    return null;
  }
  return (
    <div className="sources">
      <h4>Sources</h4>
      <div className="sources-strip">
        {sources.map((src, i) => {
          const hasPage = typeof src.page === "number" && src.page > 0;
          const label = hasPage ? `${src.document} · p.${src.page}` : src.document;
          return (
            <a
              key={`${src.document_id}-${i}`}
              className="source-link"
              href={documentFileUrl(src.document_id, src.page)}
              target="_blank"
              rel="noopener noreferrer"
              title={src.excerpt}
            >
              <span className="source-link-label">{label}</span>
            </a>
          );
        })}
      </div>
    </div>
  );
}
