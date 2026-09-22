import { defineConfig } from "vite";

/**
 * 构建产物由 Python 后端（webui/api.py）以静态文件托管，
 * 因此资源路径用相对路径（base: "./"），便于任意挂载路径。
 *
 * 注意：不要用 file:// 直接打开 dist/index.html —— 界面的所有数据都来自
 * 本地服务的 /api 接口，而 api.py 只放行本机来源（file:// 会带 Origin: null
 * 被拒）。请始终通过 `python -m webui.desktop` 或 `python -m webui.api` 访问。
 */
export default defineConfig({
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2020",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
});
