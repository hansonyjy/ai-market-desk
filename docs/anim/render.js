// Renders docs/anim/desk.html to docs/img/desk.gif, frame by frame, plus
// docs/img/social-preview.png, the still GitHub shows when the repo link is shared.
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
  // A still for GitHub's social preview (1280x640): the finished frame, centred.
  const still = await browser.newPage({ viewport: { width: 1280, height: 640 }, deviceScaleFactor: 1 });
  await still.goto("file://" + path.join(__dirname, "desk.html"));
  await still.evaluate(() => document.fonts.ready);
  await still.evaluate(() => {
    render(DURATION - 0.6);
    document.getElementById("bar").parentElement.style.display = "none";
    const s = 640 / (window.HEIGHT || 640);
    document.documentElement.style.cssText = "width:1280px;height:640px;background:linear-gradient(135deg,#0b1220,#16213b)";
    Object.assign(document.body.style, { transform: `scale(${s})`, transformOrigin: "0 0", position: "absolute",
                                         left: `${(1280 - 1200 * s) / 2}px`, top: "0px" });
  });
  await still.screenshot({ path: path.join(root, "docs", "img", "social-preview.png") });
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
