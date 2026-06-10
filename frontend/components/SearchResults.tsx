"use client";

import { useCallback, useState } from "react";
import { documentFileUrl, search, type SearchResult } from "@/services/api";

export default function SearchResults() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searched, setSearched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSearch = useCallback(async () => {
    const trimmed = query.trim();
    if (!trimmed) {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await search(trimmed);
      setResults(res.results);
      setSearched(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [query]);

  return (
    <section className="card">
      <h2>Semantic Search</h2>

      <div className="row">
        <input
          type="text"
          value={query}
          placeholder="Search documents..."
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !loading) {
              void onSearch();
            }
          }}
          disabled={loading}
        />
        <button onClick={() => void onSearch()} disabled={loading || !query.trim()}>
          {loading ? "Searching..." : "Search"}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {searched && results.length === 0 && !loading && <p className="muted">No results found.</p>}

      {results.length > 0 && (
        <ol className="search-list">
          {results.map((r) => (
            <li key={r.chunk_id}>
              <a
                className="source-link"
                href={documentFileUrl(r.document_id)}
                target="_blank"
                rel="noopener noreferrer"
                title={r.excerpt}
              >
                <span className="source-link-label">{r.document_name}</span>
                <span className="source-link-score">{r.score.toFixed(3)}</span>
              </a>
              <p className="excerpt">{r.excerpt}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
