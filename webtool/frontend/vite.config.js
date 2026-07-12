import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    headers: {
      "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
    },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
      },
    },
  },
  preview: {
    headers: {
      "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
