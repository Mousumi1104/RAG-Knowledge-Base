import { documentFileUrl, type ChatSource } from "@/services/api";

export default function Sources({ sources }: { sources: ChatSource[] }) {
  if (sources.length === 0) {
    return null;
  }
  return (
    <div className="sources">
      <h4>Sources</h4>
      <ol>
        {sources.map((src, i) => {
          const hasPage = typeof src.page === "number" && src.page > 0;
          const label = hasPage ? `${src.document} · p.${src.page}` : src.document;
          return (
            <li key={`${src.document_id}-${i}`}>
              <a
                className="source-link"
                href={documentFileUrl(src.document_id, src.page)}
                target="_blank"
                rel="noopener noreferrer"
                title={src.excerpt}
              >
                <span className="source-link-label">{label}</span>
                <span className="source-link-score">{src.score.toFixed(3)}</span>
              </a>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
