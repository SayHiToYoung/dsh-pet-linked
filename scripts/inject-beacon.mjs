#!/usr/bin/env node
// dsh-work-beacon 注入器 —— 把工作状态信标注入 DSH 前端（备份 + 可回滚）。
//
// 用法:
//   node scripts/inject-beacon.mjs --target "<DSH_INSTALL_DIR>"   # 注入
//   node scripts/inject-beacon.mjs --rollback "<backup dir>"      # 还原
//
// 目标目录通常是 DSH 安装目录（含 node_modules/@deepseek-ai/dsh-web-frontend/dist）。
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";

const EXT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const MARKER = "DSH-WORK-BEACON v1";
const DEFAULT_TARGET = process.env.DSH_INSTALL_DIR || ".";
const BACKUP_ROOT = process.env.DSH_WORK_BEACON_BACKUP || path.join(os.tmpdir(), "dsh-work-beacon-backup");

const BEACON_REL = "node_modules/@deepseek-ai/dsh-web-frontend/dist/assets/dsh-work-beacon.js";
const INDEX_REL = "node_modules/@deepseek-ai/dsh-web-frontend/dist/index.html";
const INJECT_HTML =
  `    <script src="/assets/dsh-work-beacon.js" data-dsh-work-beacon></script>`;

function read(file) { return fs.readFileSync(file, "utf8"); }
function write(file, content) { fs.writeFileSync(file, content, "utf8"); }
function sha256(content) { return crypto.createHash("sha256").update(content).digest("hex"); }
function timestamp() { return new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 17); }

function patchIndexHtml(source) {
  if (source.includes(MARKER)) return { source, changed: false };
  const tag = `<script src="/assets/dsh-work-beacon.js"`;
  if (source.includes(tag)) {
    // 已注入但缺 marker：补上 marker（幂等修复）
    const first = source.indexOf(tag);
    const marker = `<!-- ${MARKER} -->\n`;
    return { source: source.slice(0, first) + marker + source.slice(first), changed: true };
  }
  const marker = `  <!-- ${MARKER} -->\n${INJECT_HTML}`;
  const anchor = "</head>";
  const at = source.lastIndexOf(anchor);
  if (at === -1) throw new Error("index.html: </head> anchor not found");
  return { source: source.slice(0, at) + marker + "\n" + source.slice(at), changed: true };
}

function plan(target) {
  const base = path.resolve(target);
  const beaconSrc = path.join(EXT, "beacon", "dsh-work-beacon.js");
  if (!fs.existsSync(beaconSrc)) throw new Error(`beacon missing: ${beaconSrc}`);
  const assetsRoot = path.join(base, "node_modules/@deepseek-ai/dsh-web-frontend/dist", "assets");
  const writes = [
    { rel: INDEX_REL, content: patchIndexHtml(read(path.join(base, INDEX_REL))).source },
    { rel: BEACON_REL, content: fs.readFileSync(beaconSrc) },
  ];
  return { base, assetsRoot, writes };
}

function apply(target = DEFAULT_TARGET) {
  const { base, writes } = plan(target);
  const records = writes.map((item, index) => {
    const dest = path.join(base, ...item.rel.split("/"));
    const originalExists = fs.existsSync(dest);
    const original = originalExists ? fs.readFileSync(dest) : null;
    return { ...item, dest, index, originalExists, original,
             backupName: originalExists ? `file-${String(index).padStart(2, "0")}-${path.basename(dest)}` : null,
             originalSha256: original === null ? null : sha256(original),
             patchedSha256: sha256(item.content) };
  });
  // 已应用判定：html 已带 marker 且 beacon 已存在
  const htmlApplied = read(path.join(base, INDEX_REL)).includes(MARKER);
  if (htmlApplied && fs.existsSync(path.join(base, BEACON_REL))) {
    console.log("[dsh-work-beacon] already applied.");
    return "already";
  }
  const backupDir = path.join(BACKUP_ROOT, `dsh-work-beacon-${timestamp()}`);
  fs.mkdirSync(backupDir, { recursive: true });
  for (const r of records) {
    if (r.originalExists) {
      fs.mkdirSync(path.dirname(path.join(backupDir, r.backupName)), { recursive: true });
      fs.writeFileSync(path.join(backupDir, r.backupName), r.original);
    }
  }
  fs.writeFileSync(path.join(backupDir, "manifest.json"), JSON.stringify({
    marker: MARKER, target: base, createdAt: new Date().toISOString(),
    files: records.map(({ dest, backupName, originalExists, originalSha256, patchedSha256 }) =>
      ({ dest, backupName, originalExists, originalSha256, patchedSha256 })),
  }, null, 2), "utf8");
  for (const r of records) {
    fs.mkdirSync(path.dirname(r.dest), { recursive: true });
    fs.writeFileSync(r.dest, r.content);
  }
  console.log(`[dsh-work-beacon] Applied to ${base}`);
  console.log(`[dsh-work-beacon] Backup: ${backupDir}`);
  return backupDir;
}

function rollback(backupDir) {
  const manifestPath = path.join(backupDir, "manifest.json");
  if (!fs.existsSync(manifestPath)) throw new Error(`manifest not found: ${manifestPath}`);
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  if (manifest.marker !== MARKER || !Array.isArray(manifest.files)) throw new Error(`unsupported manifest: ${manifestPath}`);
  for (const record of manifest.files) {
    if (record.originalExists) {
      const backup = fs.readFileSync(path.join(backupDir, record.backupName));
      if (sha256(backup) !== record.originalSha256) throw new Error(`checksum mismatch: ${record.dest}`);
      fs.mkdirSync(path.dirname(record.dest), { recursive: true });
      fs.writeFileSync(record.dest, backup);
    } else if (fs.existsSync(record.dest)) {
      fs.unlinkSync(record.dest);
    }
  }
  console.log(`[dsh-work-beacon] Rolled back: ${backupDir}`);
  return backupDir;
}

const isDirectRun = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isDirectRun) {
  const args = process.argv.slice(2);
  const rollbackIdx = args.indexOf("--rollback");
  const targetIdx = args.indexOf("--target");
  if (rollbackIdx >= 0) {
    const dir = args[rollbackIdx + 1];
    if (!dir) { console.error("usage: --rollback <backupDir>"); process.exit(2); }
    rollback(path.resolve(dir));
  } else {
    const target = targetIdx >= 0 ? args[targetIdx + 1] : DEFAULT_TARGET;
    if (!fs.existsSync(target)) { console.error(`target not found: ${target}`); process.exit(2); }
    apply(target);
  }
}
