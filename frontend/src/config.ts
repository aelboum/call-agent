/**
 * Frontend configuration.
 *
 * Everything here is build-time public. No secret, no credential, no API key
 * ever belongs in this file or in any `VITE_` variable.
 *
 * The commercial product name is deliberately undecided (ADR-0005), so the
 * display name is never hardcoded: it comes from the API's `/v1/meta`, which
 * reads it from the deployment's own configuration. The value below is only a
 * neutral placeholder shown before that response arrives.
 */
export const config = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
  fallbackDisplayName: import.meta.env.VITE_APP_DISPLAY_NAME ?? "Console",
} as const;
