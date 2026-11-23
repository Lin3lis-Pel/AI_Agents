import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";

export default defineConfig({
  plugins: [tsconfigPaths(), react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules", ".next", "e2e"],
    coverage: {
      provider: "v8",
      include: ["components/**/*.tsx", "lib/**/*.ts"],
      exclude: ["**/*.test.*", "lib/fixtures.ts", "lib/*.d.ts", "lib/types.ts"],
      // The measured values when this was added, rounded down.
      thresholds: { statements: 85, branches: 75, functions: 85, lines: 85 },
    },
  },
});
