import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Node provides this at config time; the app itself has no Node types.
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": "/src" } },
  server: {
    port: 5173,
    host: "127.0.0.1",
    // RAPPORT_PORT matches `rapport serve --port` (scripts/dev-local.ps1) when 7860 is taken.
    proxy: { "/api": { target: `http://127.0.0.1:${process.env.RAPPORT_PORT || "7860"}`, changeOrigin: false } },
  },
  build: {
    outDir: "../backend/rapport/static",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: { output: { manualChunks: { echarts: ["echarts"] } } },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
} as any);
