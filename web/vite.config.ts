/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the console talks to the api on :8080. In the container nginx does the same job.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/api": { target: "http://localhost:8080", ws: true },
    },
  },
  test: {
    environment: "jsdom",
  },
});
