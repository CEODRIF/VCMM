import { fileURLToPath } from "node:url";
import path from "node:path";
import { defineConfig } from "vitest/config";

const rootDir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  // The `server-only` package maps its react-server condition to an empty
  // module, which is what plain Node (vitest) needs to import server modules.
  resolve: {
    alias: {
      "@": path.resolve(rootDir, "src"),
    },
    conditions: ["react-server"],
  },
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});
