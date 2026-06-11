import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Single-port serve: build to the backend's static area; FastAPI mounts it last.
// base "./" keeps asset paths relative so the SAME build works at "/" AND at "/v2"
// (the non-breaking preview mount while we reach parity with the old UI).
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../static_v2", emptyOutDir: true },
  server: {
    port: 5173,                                   // dev only -- hot reload, proxied to the backend
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
