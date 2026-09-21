import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The frontend is independent of the Python package: it never imports from it
// and never shares a build. In development it proxies `/v1` and the health
// endpoints to the local API so the browser makes same-origin requests and no
// CORS configuration is needed for local work.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/v1": { target: "http://localhost:8000", changeOrigin: true },
      "/healthz": { target: "http://localhost:8000", changeOrigin: true },
      "/readyz": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
