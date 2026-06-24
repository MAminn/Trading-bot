// @lovable.dev/vite-tanstack-config already includes the following — do NOT add them manually
// or the app will break with duplicate plugins:
//   - tanstackStart, viteReact, tailwindcss, tsConfigPaths, nitro (build-only using cloudflare as a default target),
//     componentTagger (dev-only), VITE_* env injection, @ path alias, React/TanStack dedupe,
//     error logger plugins, and sandbox detection (port/host/strictPort).
// You can pass additional config via defineConfig({ vite: { ... }, etc... }) if needed.
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

export default defineConfig({
  tanstackStart: {
    // Redirect TanStack Start's bundled server entry to src/server.ts (our SSR error wrapper).
    // nitro/vite builds from this
    server: { entry: "server" },
  },
  // Force-enable Nitro with the standalone Node server preset for self-hosted
  // (Hostinger VPS) production. Without this, a non-Lovable build skips Nitro
  // entirely and never emits a runnable Node server that listens on a port.
  // The `node-server` preset emits `.output/server/index.mjs`, started with
  // `node .output/server/index.mjs` (honours PORT, defaults to 3000).
  nitro: { preset: "node-server" },
});
