import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy /api to a FastAPI backend. Defaults to a local uvicorn on :8000, but set
// API_PROXY_TARGET to point at a deployed API (e.g. an internal Azure Container App over VPN).
// In the container, nginx proxies /api to the API container app instead.
const apiTarget = process.env.API_PROXY_TARGET || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        // Internal ACA ingress may present a cert the local Node client won't validate.
        secure: false,
      },
    },
  },
});
