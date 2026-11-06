import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The client is served by the Python server from src/terrascope/static, under
// any path prefix (e.g. behind jupyter-server-proxy), so all URLs are relative.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "../src/terrascope/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
