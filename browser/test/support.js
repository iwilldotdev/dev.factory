// Helpers of the runner tests: a local static server on port 0, temp directories and PNG fixtures. No network, no external files.
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { PNG } from "pngjs";

export const CLI = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src", "cli.js");

export const PAGES = {
  "/index.html": `<html><body style="margin:0"><div id="box" style="width:100px;height:40px;background:#c81e1e"></div>
    <h1 id="t" style="margin:0;font:600 20px/28px Arial;color:#1f2933">No invoices yet</h1>
    <button id="open" onclick="document.getElementById('drawer').style.display='block'">Open</button>
    <div id="drawer" style="display:none;width:60px;height:30px;background:#0000ff">drawer</div></body></html>`,
  "/plain.html": `<html><body style="margin:0"><p id="p" style="margin:0;font:16px Arial">x</p><div id="b" style="width:50px;height:20px;border:0"></div>
    <h2 id="empty" style="line-height:normal"></h2></body></html>`,
  "/login.html": `<html><body><input id="u"><input id="p" type="password"><button id="go" onclick="document.cookie='sid=ok-'+document.getElementById('u').value+'; path=/';location='/home.html'">Go</button></body></html>`,
  "/home.html": `<html><body><h1 id="who"></h1><script>document.getElementById('who').textContent=(document.cookie.match(/sid=([^;]+)/)||[])[1]||'anonymous'</script></body></html>`,
  "/away.html": `<html><body><script>location='http://127.0.0.2:9/'</script></body></html>`,
};

export async function serve() {
  const server = http.createServer((req, res) => {
    const page = PAGES[req.url.split("?")[0]];
    res.writeHead(page ? 200 : 404, { "content-type": "text/html" });
    res.end(page || "not found");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((r) => server.close(r)) };
}

export const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "fac tory's browser "));

export function solid(w, h, [r, g, b] = [200, 30, 30]) {
  const png = new PNG({ width: w, height: h });
  for (let i = 0; i < w * h; i++) png.data.set([r, g, b, 255], i * 4);
  return png;
}

export const writePng = (file, png) => fs.writeFileSync(file, PNG.sync.write(png));
export const writeJson = (file, obj) => (fs.writeFileSync(file, JSON.stringify(obj)), file);

/** Run the CLI as a separate process, the way the Python side does (asynchronously, so this process can serve its pages). */
export function cli(args, env = {}) {
  const out = tmp();
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [CLI, ...args, "--out", out], { env: { ...process.env, ...env }, stdio: ["ignore", "pipe", "pipe"] });
    let text = "";
    child.stdout.on("data", (d) => (text += d));
    child.stderr.on("data", (d) => (text += d));
    const timer = setTimeout(() => child.kill(), 60000);
    child.on("close", (code) => {
      clearTimeout(timer);
      const file = path.join(out, "result.json");
      resolve({ code, out, result: fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : null, text });
    });
  });
}
