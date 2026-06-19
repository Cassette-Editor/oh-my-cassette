import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built assets are served by FastAPI under /static, so the public base must match.
// `npm run dev` proxies /api to the local FastAPI service (default :8088).
export default defineConfig({
  base: "/static/",
  plugins: [react()],
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8088",
    },
  },
});
