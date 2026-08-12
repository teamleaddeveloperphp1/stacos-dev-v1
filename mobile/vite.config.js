import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3002,
    // Proxy to Django so the browser sees a single origin during development
    // and CORS stays out of the way. Native builds talk to the real host and
    // never hit this path.
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    // Capacitor serves from a file:// origin, so assets must be relative.
    assetsDir: "assets",
    sourcemap: false,
  },
  base: "",
});
