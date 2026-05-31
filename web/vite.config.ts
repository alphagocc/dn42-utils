import { resolve } from "path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      input: {
        admin: resolve(__dirname, "admin/index.html"),
        peer: resolve(__dirname, "peer/index.html"),
      },
    },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://[::1]:4242",
        changeOrigin: true,
      },
    },
  },
});
