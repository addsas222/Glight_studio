/**
 * CDP screenshot driver for static HTML files (the .pen -> HTML exports).
 *
 * usage: node shot_url.mjs <W> <H> <url> <outPng> [<url> <outPng> ...]
 */
import { writeFileSync } from "node:fs";

const [W, H, ...pairs] = process.argv.slice(2);
const width = Number(W);
const height = Number(H);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function findTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch("http://127.0.0.1:9222/json/list")).json();
      const page = list.find((t) => t.type === "page");
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch {
      /* browser not up yet */
    }
    await sleep(500);
  }
  throw new Error("no debuggable page target");
}

const ws = new WebSocket(await findTarget());
await new Promise((res, rej) => {
  ws.onopen = res;
  ws.onerror = rej;
});

let seq = 0;
const pending = new Map();
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  }
};

function send(method, params = {}) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(`timeout: ${method}`));
      }
    }, 120000);
  });
}

async function evalJs(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width, height, deviceScaleFactor: 2, mobile: false,
});

for (let i = 0; i < pairs.length; i += 2) {
  const [url, out] = pairs.slice(i, i + 2);
  await send("Page.navigate", { url });
  await sleep(1200);
  await evalJs("document.fonts ? document.fonts.ready.then(()=>1) : 1");
  await sleep(600);
  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(out, Buffer.from(shot.data, "base64"));
  console.log(`shot ${url} -> ${out} (${Buffer.from(shot.data, "base64").length} bytes)`);
}

ws.close();
process.exit(0);
