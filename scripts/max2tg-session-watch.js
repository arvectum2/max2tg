#!/opt/homebrew/bin/node
"use strict";
const fs = require("fs");
const path = require("path");
const os = require("os");
const cp = require("child_process");

const envPath = process.env.MAX2TG_WATCH_ENV ||
  "/Volumes/ArvectumSSD/Arvectum/arvectum-max-bridge/.env";
const chromeRoot = path.join(os.homedir(),
  "Library/Application Support/Google/Chrome");
const storageOverride = process.env.MAX2TG_WATCH_STORAGE || "";

function log(s) {
  process.stdout.write(new Date().toISOString() + " " + s + "\n");
}

function maxStorageDir() {
  if (storageOverride) return storageOverride;
  try {
    const state = JSON.parse(fs.readFileSync(path.join(chromeRoot, "Local State"), "utf8"));
    const cache = state.profile && state.profile.info_cache || {};
    for (const [dir, info] of Object.entries(cache)) {
      if (String(info.name || "").trim().toUpperCase() === "MAX") {
        return path.join(chromeRoot, dir, "Local Storage", "leveldb");
      }
    }
  } catch (_) {}
  return path.join(chromeRoot, "Profile 4", "Local Storage", "leveldb");
}

function authCandidates() {
  const storageDir = maxStorageDir();
  if (!fs.existsSync(storageDir)) return [];
  const out = [];
  for (const name of fs.readdirSync(storageDir)) {
    if (!name.endsWith(".log") && !name.endsWith(".ldb")) continue;
    const file = path.join(storageDir, name);
    let data, stat;
    try {
      data = fs.readFileSync(file);
      stat = fs.statSync(file);
    } catch (_) {
      continue;
    }

    const marker = Buffer.from("__oneme_auth");
    let at = 0;
    while ((at = data.indexOf(marker, at)) !== -1) {
      const tail = data.subarray(at, Math.min(data.length, at + 10000))
        .toString("utf8");
      const m = tail.match(/\{\s*"token"\s*:\s*"([^"]+)"[^}]*"viewerId"\s*:\s*([0-9]+)[^}]*\}/);
      if (m && m[1].length > 200) {
        out.push({ token: m[1], viewerId: m[2], mtime: stat.mtimeMs, at });
      }
      at += marker.length;
    }
  }
  return out.sort((a, b) => (b.mtime - a.mtime) || (b.at - a.at));
}

function currentToken(envText) {
  const m = envText.match(/^MAX_TOKEN=(.*)$/m);
  return m ? m[1].trim() : "";
}

function writeEnv(envText, token) {
  const next = /^MAX_TOKEN=.*$/m.test(envText)
    ? envText.replace(/^MAX_TOKEN=.*$/m, "MAX_TOKEN=" + token)
    : "MAX_TOKEN=" + token + "\n" + envText;
  const tmp = envPath + ".session-watch.tmp";
  fs.writeFileSync(tmp, next, { mode: 0o600 });
  fs.chmodSync(tmp, 0o600);
  fs.renameSync(tmp, envPath);
}

function restartBridge() {
  if (process.env.MAX2TG_WATCH_NO_RESTART === "1") return true;
  const uid = process.getuid();
  const target = "gui/" + uid + "/com.arvectum.max2tg";
  const first = cp.spawnSync("/bin/launchctl", ["kickstart", "-k", target],
    { stdio: "ignore" });
  if (first.status === 0) return true;

  const plist = "/Users/master/Library/LaunchAgents/com.arvectum.max2tg.plist";
  cp.spawnSync("/bin/launchctl", ["bootstrap", "gui/" + uid, plist],
    { stdio: "ignore" });
  const second = cp.spawnSync("/bin/launchctl", ["kickstart", "-k", target],
    { stdio: "ignore" });
  return second.status === 0;
}

function main() {
  if (!fs.existsSync(envPath)) return 0;
  const candidates = authCandidates();
  if (!candidates.length) return 0;

  const envText = fs.readFileSync(envPath, "utf8");
  const oldToken = currentToken(envText);
  const fresh = candidates[0];
  if (!oldToken || oldToken === fresh.token) return 0;

  writeEnv(envText, fresh.token);
  log("MAX session changed; bridge config refreshed");

  if (restartBridge()) {
    log("MAX2TG restart requested");
    return 0;
  }
  log("ERROR: MAX2TG restart failed");
  return 1;
}

try {
  process.exitCode = main();
} catch (e) {
  log("ERROR: session watcher failed: " + e.message);
  process.exitCode = 1;
}
