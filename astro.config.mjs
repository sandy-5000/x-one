import { defineConfig } from "astro/config";
import rehypeNoteBlocks from "./src/plugins/rehype-note-blocks.mjs";

export default defineConfig({
  // Astro copies publicDir into the build, so the build output can't also be ./public.
  publicDir: "./static",
  outDir: "./public",
  build: { format: "file" },
  markdown: {
    syntaxHighlight: false,
    rehypePlugins: [rehypeNoteBlocks],
  },
});
