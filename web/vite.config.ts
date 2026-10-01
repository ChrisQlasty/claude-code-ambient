import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

// The daemon serves the build from the Python package, so users never need Node.
// `npm run dev` proxies the API to a daemon started with `ambient daemon start`.
export default defineConfig({
  plugins: [svelte()],
  base: "./",
  build: {
    outDir: "../src/ambient/web_dist",
    emptyOutDir: true,
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
});
