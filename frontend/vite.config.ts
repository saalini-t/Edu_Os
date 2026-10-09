import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy: the browser talks only to the Vite origin; /v1 is forwarded to the backend.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/v1": "http://localhost:8000" } },
});
