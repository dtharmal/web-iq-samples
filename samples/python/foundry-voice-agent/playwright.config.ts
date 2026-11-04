// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import { defineConfig } from "@playwright/test";

const port = process.env.TEST_PORT ?? "8766";
export default defineConfig({
  testDir: "./tests",
  testMatch: "browser.spec.ts",
  workers: 1,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    permissions: ["microphone"],
    launchOptions: { args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] },
  },
  webServer: {
    command: ".venv/bin/python -m app.main",
    url: `http://127.0.0.1:${port}/api/health`,
    env: {
      PORT: port, PERF_LOG_ENABLED: "0", APP_MODE: "local", LOCAL_AGENT_VALIDATION_ENABLED: "0",
      // Placeholders keep browser tests independent of a developer's .env; voice sockets are mocked.
      FOUNDRY_PROJECT_ENDPOINT: "https://example-resource.services.ai.azure.com/api/projects/example-project",
      FOUNDRY_VOICE_AGENT_NAME: "offline-test-agent", FOUNDRY_VOICE_AGENT_VERSION: "1",
      WEBIQ_API_KEY: "offline-test-placeholder",
    },
    reuseExistingServer: false,
  },
});
