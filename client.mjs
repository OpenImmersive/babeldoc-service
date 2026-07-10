// OpenImmersive / yulingling — Node client for the BabelDOC wrapper service.
// Usage:   node client.mjs <input.pdf> [output.pdf] [pages] [lang_out]
// Example: node client.mjs paper.pdf paper.zh.pdf 1-2 zh
//
// This is the pattern server.mjs should use when routing scanned/complex
// documents to the BabelDOC engine: upload -> poll -> download.

import { readFile, writeFile } from "node:fs/promises";
import { basename } from "node:path";

const BASE = process.env.BABELDOC_URL || "http://127.0.0.1:21012";

export async function translatePdf(inputPath, { langOut = "zh", pages = "", qps = 2, variant = "mono", pollMs = 3000, timeoutMs = 30 * 60 * 1000 } = {}) {
  // 1. upload
  const form = new FormData();
  form.append("file", new Blob([await readFile(inputPath)], { type: "application/pdf" }), basename(inputPath));
  form.append("lang_out", langOut);
  if (pages) form.append("pages", pages);
  form.append("qps", String(qps));

  const submit = await fetch(`${BASE}/translate`, { method: "POST", body: form });
  if (!submit.ok) throw new Error(`submit failed: ${submit.status} ${await submit.text()}`);
  const { id } = await submit.json();

  // 2. poll
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (Date.now() > deadline) throw new Error(`job ${id} timed out`);
    await new Promise((r) => setTimeout(r, pollMs));
    const res = await fetch(`${BASE}/status/${id}`);
    if (!res.ok) throw new Error(`status failed: ${res.status}`);
    const st = await res.json();
    if (st.status === "done") break;
    if (st.status === "failed") throw new Error(`job ${id} failed: ${st.error}\n--- log tail ---\n${st.log_tail}`);
  }

  // 3. download
  const dl = await fetch(`${BASE}/result/${id}?variant=${variant}`);
  if (!dl.ok) throw new Error(`download failed: ${dl.status}`);
  return Buffer.from(await dl.arrayBuffer());
}

// CLI entry
if (import.meta.url === `file://${process.argv[1]}`) {
  const [input, output = "translated.pdf", pages = "", langOut = "zh"] = process.argv.slice(2);
  if (!input) {
    console.error("usage: node client.mjs <input.pdf> [output.pdf] [pages] [lang_out]");
    process.exit(1);
  }
  const buf = await translatePdf(input, { pages, langOut });
  await writeFile(output, buf);
  console.log(`wrote ${output} (${buf.length} bytes)`);
}
