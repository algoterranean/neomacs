// Bundle the three parts of the app into out/.
import { build } from "esbuild";
import { cp, mkdir, rm } from "node:fs/promises";

await rm("out", { recursive: true, force: true });
await mkdir("out/renderer", { recursive: true });

const common = { bundle: true, sourcemap: true, logLevel: "info", target: "es2023" };

await Promise.all([
  // The main process and the preload script run in Node contexts provided by Electron.
  build({ ...common, entryPoints: ["src/main/main.ts"], outfile: "out/main.cjs",
          platform: "node", format: "cjs", external: ["electron"] }),
  // A sandboxed preload cannot use ES modules or require anything but `electron`.
  build({ ...common, entryPoints: ["src/preload/preload.ts"], outfile: "out/preload.cjs",
          platform: "node", format: "cjs", external: ["electron"] }),
  build({ ...common, entryPoints: ["src/renderer/renderer.ts"], outfile: "out/renderer/renderer.js",
          platform: "browser", format: "esm" }),
]);
await cp("src/renderer/index.html", "out/renderer/index.html");
await cp("src/renderer/styles.css", "out/renderer/styles.css");
