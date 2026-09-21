/**
 * The API connectivity path.
 *
 * The frontend is independent of the backend package: it shares no code with
 * it and talks to it only over HTTP. This module is the single place that
 * knows the API's shape, so a route change is a one-file change here.
 */
import { config } from "./config";

export interface Meta {
  name: string;
  version: string;
  api_version: string;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${config.apiBaseUrl}${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`${path} responded ${response.status}`);
  }
  return (await response.json()) as T;
}

export function fetchMeta(): Promise<Meta> {
  return getJson<Meta>("/v1/meta");
}

export async function fetchLiveness(): Promise<string> {
  const body = await getJson<{ status: string }>("/healthz");
  return body.status;
}
