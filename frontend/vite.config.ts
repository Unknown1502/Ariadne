import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // The console never holds application state of its own; it reads the ledger through
      // the API. Proxying in dev keeps that single origin.
      "/api": { target: "http://localhost:8080", changeOrigin: true },
      "/health": { target: "http://localhost:8080", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
