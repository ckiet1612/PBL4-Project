import { defineConfig } from "vitest/config";

// Unit tests are pure logic and server-rendered markup: no network, no DOM emulation.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    restoreMocks: true,
  },
});
