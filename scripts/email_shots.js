// Screenshots of the demo's emails for the README gallery.
// Run after `desk demo`:  node scripts/email_shots.js [out/demo/mail] [docs/img]
// Each HTML email is shown as a mail client would: white card, the email's own
// width, cropped to its content. Long emails are cut after MAX_H pixels.
const { chromium } = require("playwright");
const fs = require("fs"), path = require("path");

const root = path.resolve(__dirname, "..");
const src = path.resolve(process.argv[2] || path.join(root, "out/demo/mail"));
const dst = path.resolve(process.argv[3] || path.join(root, "docs/img"));
const MAX_H = 900;
const NAMES = [[/opening-digest/, "email-digest"], [/^\d+-vip-/, "email-vip"], [/^\d+-re-/, "email-ask"],
               [/alerts?-|down-|up-/, "email-alert"]];

(async () => {
  const files = fs.readdirSync(src).filter(f => f.endsWith(".html")).sort();
  const txt = fs.readdirSync(src).filter(f => f.endsWith(".txt")).sort();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 760, height: 1200 }, deviceScaleFactor: 2 });
  const done = new Set();
  for (const f of [...files, ...txt.filter(t => !files.includes(t.replace(/\.txt$/, ".html")))]) {
    const kind = (NAMES.find(([rx]) => rx.test(f)) || [, "email"])[1];
    if (done.has(kind)) continue;   // one of each kind
    done.add(kind);
    const raw = fs.readFileSync(path.join(src, f), "utf8");
    const txtTwin = path.join(src, f.replace(/\.html$/, ".txt"));   // the HTML part has no headers
    const head = fs.existsSync(txtTwin) ? fs.readFileSync(txtTwin, "utf8") : raw;
    const subject = (head.match(/^Subject: (.*)$/m) || [, ""])[1] || f;
    // A multi-alert email shows its first alert only (the rest follow the same shape).
    const html = raw.replace(/^[\s\S]*?(<div)/, "$1");
    const first = html.indexOf("<hr");
    const body = f.endsWith(".html") ? (first > 0 ? html.slice(0, first) + "</div>" : html)
      : `<pre style="white-space:pre-wrap;font:14px/1.5 Arial,Helvetica,sans-serif;margin:0">${raw.replace(/^Subject:.*\n(To:.*\n)?\n?/, "")
          .replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))}</pre>`;
    await page.setContent(`<!doctype html><meta charset="utf-8"><body style="margin:0;background:#eef1f6;padding:24px">
      <div id="card" style="background:#fff;border-radius:12px;box-shadow:0 4px 18px rgba(15,23,42,.12);padding:22px 26px;max-width:680px">
        <div style="font:600 17px/1.3 Arial,Helvetica,sans-serif;color:#111827;margin-bottom:4px">${subject.replace(/[&<>]/g, "")}</div>
        <div style="font:13px Arial,Helvetica,sans-serif;color:#6b7280;margin-bottom:16px">AI Market Desk &middot; to you</div>
        ${body}</div></body>`);
    const card = await page.$("#card");
    const box = await card.boundingBox();
    const out = path.join(dst, `${kind}.png`);
    await page.screenshot({ path: out, clip: { x: box.x - 12, y: box.y - 12, width: box.width + 24, height: Math.min(box.height + 24, MAX_H) } });
    console.log(`${out}  <- ${f}`);
  }
  await browser.close();
})();
