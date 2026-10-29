// Renders docs/anim/desk.html to docs/img/desk.gif, frame by frame.
// Run: node docs/anim/render.js   (needs Playwright with Chromium, and ffmpeg)
const { chromium } = require("playwright");
const { execFileSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");

const FPS = Number(process.env.FPS || 20);
const root = path.resolve(__dirname, "..", "..");
const out = path.join(root, "docs", "img", "desk.gif");
const frames = fs.mkdtempSync(path.join(os.tmpdir(), "desk-frames-"));

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 640 }, deviceScaleFactor: 1 });
  await page.goto("file://" + path.join(__dirname, "desk.html"));
  await page.setViewportSize({ width: 1200, height: await page.evaluate(() => window.HEIGHT || 640) });
  await page.evaluate(() => document.fonts.ready);
  const duration = await page.evaluate(() => window.DURATION);
  const n = Math.round(duration * FPS);
  for (let i = 0; i < n; i++) {
    await page.evaluate(t => window.render(t), i / FPS);
    await page.screenshot({ path: path.join(frames, `f${String(i).padStart(4, "0")}.png`) });
  }
  await browser.close();
  // One palette for the whole GIF keeps colours steady; no dithering keeps flat areas small.
  const pal = path.join(frames, "palette.png");
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-framerate", String(FPS), "-i", path.join(frames, "f%04d.png"),
    "-vf", "palettegen=max_colors=128:stats_mode=full", pal]);
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-framerate", String(FPS), "-i", path.join(frames, "f%04d.png"),
    "-i", pal, "-lavfi", "paletteuse=dither=none:diff_mode=rectangle", "-loop", "0", out]);
  fs.rmSync(frames, { recursive: true, force: true });
  console.log(`${out}: ${n} frames, ${(fs.statSync(out).size / 1e6).toFixed(2)} MB`);
})();
