import { useEffect, useState } from "react";

import { fetchLiveness, fetchMeta, type Meta } from "./api";
import { config } from "./config";

/**
 * The Phase 1 frontend scaffold.
 *
 * It exists to prove one thing end to end: the frontend builds, runs, and can
 * reach the API. It deliberately commits to no visual design, and contains no
 * CRM screen, no marketing page and no agent builder -- those belong to later
 * phases, and a placeholder design would only have to be thrown away.
 */
export function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [liveness, setLiveness] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    Promise.all([fetchMeta(), fetchLiveness()])
      .then(([metaResponse, livenessResponse]) => {
        if (cancelled) return;
        setMeta(metaResponse);
        setLiveness(livenessResponse);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : "unknown error");
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const displayName = meta?.name ?? config.fallbackDisplayName;

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem", lineHeight: 1.5 }}>
      <h1>{displayName}</h1>
      {error ? (
        <p role="alert">API unreachable: {error}</p>
      ) : (
        <dl>
          <dt>API</dt>
          <dd>{meta ? `${meta.api_version} (build ${meta.version})` : "checking…"}</dd>
          <dt>Liveness</dt>
          <dd>{liveness ?? "checking…"}</dd>
        </dl>
      )}
    </main>
  );
}
