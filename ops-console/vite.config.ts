import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [svelte()],
    server: {
      port: 4173,
      proxy: env.VITE_API_BASE
        ? undefined
        : {
            "/api": {
              target: "http://localhost:8000",
              changeOrigin: true
            }
          }
    }
  };
});

