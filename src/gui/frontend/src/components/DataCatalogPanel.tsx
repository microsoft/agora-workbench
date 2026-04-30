import { useState, useEffect, useCallback, useRef } from "react";
import { searchDataCatalog, fetchCatalogDomains, type CatalogAsset } from "../api";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Called when user clicks an asset — inserts the asset_tag into the chat input. */
  onInsert: (assetTag: string, name: string) => void;
}

const PAGE_SIZE = 30;

export default function DataCatalogPanel({ open, onClose, onInsert }: Props) {
  const [query, setQuery] = useState("");
  const [assets, setAssets] = useState<CatalogAsset[]>([]);
  const [configured, setConfigured] = useState(true);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Track all domains ever seen (survives pagination / search filtering)
  const [allDomains, setAllDomains] = useState<Set<string>>(new Set());

  // Gather distinct domains from results for filter chips
  const domains = Array.from(allDomains).sort();
  const [activeDomain, setActiveDomain] = useState<string | null>(null);

  // Search function
  const doSearch = useCallback(async (q: string, domain?: string) => {
    setLoading(true);
    setError(null);
    try {
      const result = await searchDataCatalog(q, domain ?? undefined, PAGE_SIZE);
      setAssets(result.assets);
      setConfigured(result.configured);
      setHasMore(result.assets.length >= PAGE_SIZE);
      // Accumulate domains from every response
      const newDomains = result.assets.map((a) => a.domain).filter(Boolean);
      if (newDomains.length > 0) {
        setAllDomains((prev) => {
          const next = new Set(prev);
          for (const d of newDomains) next.add(d);
          return next.size === prev.size ? prev : next;
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    } finally {
      setLoading(false);
    }
  }, []);

  // Load more results (skip-based pagination)
  const loadMore = useCallback(async () => {
    setLoadingMore(true);
    try {
      const currentSkip = assets.length;
      const result = await searchDataCatalog(
        query,
        activeDomain ?? undefined,
        PAGE_SIZE,
        currentSkip,
      );
      setAssets((prev) => [...prev, ...result.assets]);
      setHasMore(result.assets.length >= PAGE_SIZE);
      const newDomains = result.assets.map((a) => a.domain).filter(Boolean);
      if (newDomains.length > 0) {
        setAllDomains((prev) => {
          const next = new Set(prev);
          for (const d of newDomains) next.add(d);
          return next.size === prev.size ? prev : next;
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Load more failed");
    } finally {
      setLoadingMore(false);
    }
  }, [assets.length, query, activeDomain]);

  // Initial load when panel opens + fetch all domains
  useEffect(() => {
    if (!open) return;
    doSearch("", undefined);
    // Fetch all distinct domains from the backend
    fetchCatalogDomains()
      .then((result) => {
        if (result.domains.length > 0) {
          setAllDomains(new Set(result.domains));
        }
      })
      .catch(() => {}); // non-critical
    // Focus search box
    setTimeout(() => searchRef.current?.focus(), 100);
  }, [open, doSearch]);

  // Debounced search on query change
  useEffect(() => {
    if (!open) return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      doSearch(query, activeDomain ?? undefined);
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query, activeDomain, open, doSearch]);

  // Clear copied indicator after a delay
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(null), 1500);
    return () => clearTimeout(timer);
  }, [copied]);

  // ESC to close
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  const handleInsert = (asset: CatalogAsset) => {
    onInsert(asset.asset_tag, asset.name);
    setCopied(asset.asset_tag);
  };

  const handleDomainClick = (domain: string) => {
    setActiveDomain((prev) => (prev === domain ? null : domain));
  };

  if (!open) return null;

  // Filtered assets (client-side domain filter if domain chips are used after initial load)
  const displayAssets = activeDomain
    ? assets.filter((a) => a.domain === activeDomain)
    : assets;

  return (
    <div className="catalog-overlay" onClick={onClose}>
      <div
        className="catalog-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Data Catalog"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="catalog-header">
          <h2>Data Catalog</h2>
          <button className="catalog-close" onClick={onClose} title="Close" aria-label="Close">
            ×
          </button>
        </div>

        {/* Search bar */}
        <div className="catalog-search">
          <svg className="catalog-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            ref={searchRef}
            type="text"
            className="catalog-search-input"
            placeholder="Search datasets..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button className="catalog-search-clear" onClick={() => setQuery("")} aria-label="Clear search">
              ×
            </button>
          )}
        </div>

        {/* Domain filter chips */}
        {domains.length > 0 && (
          <div className="catalog-domains">
            <button
              className={`catalog-domain-chip ${activeDomain === null ? "catalog-domain-chip--active" : ""}`}
              onClick={() => setActiveDomain(null)}
            >
              All
            </button>
            {domains.map((d) => (
              <button
                key={d}
                className={`catalog-domain-chip ${activeDomain === d ? "catalog-domain-chip--active" : ""}`}
                onClick={() => handleDomainClick(d)}
              >
                {d}
              </button>
            ))}
          </div>
        )}

        {/* Content */}
        <div className="catalog-content">
          {!configured && (
            <div className="catalog-empty">
              <p>Data lake not configured.</p>
              <p className="catalog-empty-hint">
                Set <code>DATA_LAKE_SEARCH_ENDPOINT</code> in your <code>.env</code> to enable the catalog.
              </p>
            </div>
          )}

          {configured && loading && (
            <div className="catalog-loading">Searching...</div>
          )}

          {configured && error && (
            <div className="catalog-error">{error}</div>
          )}

          {configured && !loading && !error && displayAssets.length === 0 && (
            <div className="catalog-empty">
              <p>No datasets found{query ? ` for "${query}"` : ""}.</p>
            </div>
          )}

          {configured && !loading && displayAssets.length > 0 && (
            <>
              <ul className="catalog-list">
                {displayAssets.map((asset) => (
                  <li key={asset.asset_tag} className="catalog-item">
                    <button
                      className="catalog-item-btn"
                      onClick={() => handleInsert(asset)}
                      title="Click to insert into chat"
                    >
                      <div className="catalog-item-header">
                        <span className="catalog-item-name">{asset.name}</span>
                        {asset.domain && (
                          <span className="catalog-item-domain">{asset.domain}</span>
                        )}
                      </div>
                      {asset.description && (
                        <p className="catalog-item-desc">{asset.description}</p>
                      )}
                      {copied === asset.asset_tag && (
                        <span className="catalog-item-copied">Inserted!</span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
              {hasMore && !activeDomain && (
                <div className="catalog-load-more">
                  <button
                    className="catalog-load-more-btn"
                    onClick={loadMore}
                    disabled={loadingMore}
                  >
                    {loadingMore ? "Loading..." : "Load more"}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
