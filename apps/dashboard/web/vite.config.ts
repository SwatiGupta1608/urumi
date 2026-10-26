import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
  // Local dev: run the BFF on :8090 (it holds the session) and proxy to it.
  server: { proxy: { "/api": "http://localhost:8090", "/bff": "http://localhost:8090" } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test/setup.ts"] },
});
