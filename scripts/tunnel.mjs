#!/usr/bin/env node
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';

// ─── CLI ────────────────────────────────────────────────────────────────────

function usage(exitCode = 0) {
  const out = [
    `tunnel.mjs — tunnel subprocess manager`,
    ``,
    `  node tunnel.mjs quick --local http://127.0.0.1:8788`,
    `  node tunnel.mjs cloudflare --config /etc/cloudflared/config.yml --health http://127.0.0.1:8788/healthz`,
    `  node tunnel.mjs ngrok --local http://127.0.0.1:8788 --hostname ssh.xloud.ru`,
    ``,
    `Modes:`,
    `  quick          Cloudflare quick tunnel (trycloudflare.com, new URL each start)`,
    `  cloudflare     Cloudflare named tunnel (stable URL via config/token/name)`,
    `  ngrok          Ngrok tunnel with reserved domain`,
    ``,
    `Common options:`,
    `  --local <url>          Local server URL (default: http://127.0.0.1:8788)`,
    `  --health <url>         Full health endpoint URL (default: <local>/healthz)`,
    `  --health-path <path>   Health path instead of full URL (e.g. /mcp)`,
    `  --timeout <ms>         Max wait for tunnel ready (default: 60000)`,
    `  --verbose              Show tunnel process stdout/stderr`,
    ``,
    `Cloudflare-specific:`,
    `  --config <path>        cloudflared YAML config file path`,
    `  --token <token>        Cloudflare Tunnel token`,
    `  --token-file <path>    Cloudflare Tunnel token file`,
    `  --name <name>          Cloudflare named tunnel name`,
    `  --hostname <host>      Public hostname (for stdout URL, e.g. mcp.nodsync.org)`,
    ``,
    `Ngrok-specific:`,
    `  --hostname <host>      Ngrok reserved domain (e.g. ssh.xloud.ru)`,
    `  --ngrok-config <path>  Ngrok config file path`,
    ``,
  ].join('\n');
  console.error(out);
  process.exit(exitCode);
}

function parseArgs(argv) {
  const out = { local: 'http://127.0.0.1:8788', timeout: 60000, verbose: false };
  for (let i = 2; i < argv.length; i++) {
    const raw = argv[i];
    if (raw === '--help') usage(0);
    if (!raw.startsWith('--')) {
      if (!out.mode) { out.mode = raw; continue; }
      throw new Error(`Unexpected argument: ${raw}`);
    }
    const key = raw.slice(2);
    if (key === 'verbose') { out.verbose = true; continue; }
    const next = argv[++i];
    if (next === undefined) throw new Error(`Missing value for --${key}`);
    if (key === 'local') out.local = next;
    else if (key === 'health') out.health = next;
    else if (key === 'health-path') out.healthPath = next;
    else if (key === 'timeout') out.timeout = parseInt(next, 10);
    else if (key === 'config') out.config = next;
    else if (key === 'token') out.token = next;
    else if (key === 'token-file') out.tokenFile = next;
    else if (key === 'name') out.name = next;
    else if (key === 'hostname') out.hostname = next;
    else if (key === 'ngrok-config') out.ngrokConfig = next;
    else throw new Error(`Unknown option: --${key}`);
  }
  if (!out.mode) usage(1);
  if (!['quick', 'cloudflare', 'ngrok'].includes(out.mode)) {
    throw new Error(`Unknown mode: ${out.mode}. Use quick, cloudflare, or ngrok.`);
  }
  if (out.healthPath) {
    const base = out.local.replace(/\/+$/, '');
    const hp = out.healthPath.startsWith('/') ? out.healthPath : `/${out.healthPath}`;
    out.health = `${base}${hp}`;
  }
  if (!out.health) out.health = `${out.local.replace(/\/+$/, '')}/healthz`;
  return out;
}

// ─── Spawn helpers ──────────────────────────────────────────────────────────

const children = new Set();

function spawnLogged(name, command, args, options = {}) {
  const { verbose = false, ...spawnOptions } = options;
  const child = spawn(command, args, { ...spawnOptions, stdio: ['ignore', 'pipe', 'pipe'] });
  const logLines = [];
  const record = (stream, chunk) => {
    const text = String(chunk);
    logLines.push(...text.split(/\r?\n/).filter(Boolean).map((l) => `[${name}] ${l}`));
    while (logLines.length > 120) logLines.shift();
    if (verbose) stream.write(`[${name}] ${text}`);
  };
  child.logTail = () => logLines.join('\n');
  children.add(child);
  child.stdout.on('data', (chunk) => record(process.stdout, chunk));
  child.stderr.on('data', (chunk) => record(process.stderr, chunk));
  child.on('exit', (code, signal) => {
    children.delete(child);
    if (verbose) process.stderr.write(`[${name}] exited code=${code} signal=${signal}\n`);
  });
  return child;
}

function killProcess(child) {
  if (!child || child.killed) return;
  try { child.kill('SIGTERM'); } catch {}
  setTimeout(() => {
    if (!child.killed) try { child.kill('SIGKILL'); } catch {}
  }, 1500).unref();
}

function cleanupChildren() {
  for (const child of children) killProcess(child);
}

process.on('SIGINT', () => { cleanupChildren(); process.exit(130); });
process.on('SIGTERM', () => { cleanupChildren(); process.exit(143); });

// ─── Health check ───────────────────────────────────────────────────────────

async function sleep(ms) {
  await new Promise((r) => setTimeout(r, ms));
}

async function waitForHealth(url, timeoutMs = 15000) {
  const started = Date.now();
  let lastError = '';
  while (Date.now() - started < timeoutMs) {
    try {
      const res = await fetch(url);
      if (res.ok || res.status === 401 || res.status === 403) return;
      lastError = `${res.status} ${await res.text()}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await sleep(500);
  }
  throw new Error(`Timed out waiting for ${url}. Last error: ${lastError}`);
}

function waitForProcessExit(child) {
  return new Promise((resolve) => {
    child.once('exit', (code, signal) => resolve({ code, signal }));
  });
}

async function waitForPublicHealth(publicBase, tunnelChild, timeoutMs = 60000, tunnelLabel = 'tunnel') {
  const health = waitForHealth(`${publicBase}/healthz`, timeoutMs);
  const exit = waitForProcessExit(tunnelChild).then(({ code, signal }) => {
    throw new Error(`${tunnelLabel} exited before ${publicBase}/healthz was reachable, code=${code} signal=${signal}`);
  });
  return Promise.race([health, exit]);
}

// ─── Cloudflare quick tunnel ────────────────────────────────────────────────

function waitForCloudflareUrl(child, timeoutMs = 45000) {
  const re = /https:\/\/[a-zA-Z0-9-]+\.trycloudflare\.com/g;
  let buffer = '';
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Timed out waiting for cloudflared public URL.')), timeoutMs);
    timer.unref();
    const onData = (chunk) => {
      const text = String(chunk);
      buffer += text;
      const match = buffer.match(re);
      if (match?.[0]) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    };
    child.stdout.on('data', onData);
    child.stderr.on('data', onData);
    child.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`cloudflared exited before a URL was found, code=${code}`));
    });
  });
}

// ─── cloudflared resolution ─────────────────────────────────────────────────

function expandHome(input) {
  if (!input || input === '~') return os.homedir();
  if (input.startsWith('~/')) return path.join(os.homedir(), input.slice(2));
  return input;
}

function isPathLike(command) {
  return command.includes('/') || command.includes('\\') || command.startsWith('.');
}

function executableFileExists(filePath) {
  try { return fs.statSync(filePath).isFile(); } catch { return false; }
}

function commandExists(command) {
  const result = spawnSync(
    process.platform === 'win32' ? 'where' : 'command',
    process.platform === 'win32' ? [command] : ['-v', command],
    { shell: process.platform !== 'win32', stdio: 'ignore' }
  );
  return result.status === 0;
}

function commandAvailable(command) {
  if (isPathLike(command)) return executableFileExists(expandHome(command));
  return commandExists(command);
}

function resolveExecutablePath(command) {
  return path.resolve(expandHome(command));
}

function codexProHome() {
  const customHome = process.env.CODEXPRO_HOME;
  return customHome ? path.resolve(expandHome(customHome)) : path.join(os.homedir(), '.codexpro');
}

function localCloudflaredPath() {
  const binName = process.platform === 'win32' ? 'cloudflared.exe' : 'cloudflared';
  return path.join(codexProHome(), 'bin', binName);
}

function verifyCloudflared(binaryPath) {
  const result = spawnSync(binaryPath, ['--version'], { stdio: 'ignore', shell: false, timeout: 15000 });
  if (result.status !== 0) {
    throw new Error(`cloudflared at ${binaryPath} failed --version.`);
  }
}

function cloudflaredReleaseAsset() {
  const { platform, arch } = process;
  if (platform === 'darwin') {
    if (arch === 'arm64') return { file: 'cloudflared-darwin-arm64.tgz', archive: true };
    if (arch === 'x64') return { file: 'cloudflared-darwin-amd64.tgz', archive: true };
  }
  if (platform === 'linux') {
    if (arch === 'arm64') return { file: 'cloudflared-linux-arm64', archive: false };
    if (arch === 'arm') return { file: 'cloudflared-linux-arm', archive: false };
    if (arch === 'x64') return { file: 'cloudflared-linux-amd64', archive: false };
    if (arch === 'ia32') return { file: 'cloudflared-linux-386', archive: false };
  }
  if (platform === 'win32') {
    if (arch === 'x64') return { file: 'cloudflared-windows-amd64.exe', archive: false };
    if (arch === 'ia32') return { file: 'cloudflared-windows-386.exe', archive: false };
  }
  throw new Error(`Automatic cloudflared install not supported on ${platform}/${arch}. Install manually.`);
}

function cloudflaredBinName() {
  return process.platform === 'win32' ? 'cloudflared.exe' : 'cloudflared';
}

function findFileByName(root, fileName) {
  const entries = fs.readdirSync(root, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(root, entry.name);
    if (entry.isFile() && entry.name === fileName) return fullPath;
    if (entry.isDirectory()) {
      const found = findFileByName(fullPath, fileName);
      if (found) return found;
    }
  }
  return '';
}

async function downloadFile(url, destination) {
  const response = await fetch(url, { headers: { 'user-agent': 'tunnel-mjs' } });
  if (!response.ok) {
    throw new Error(`Failed to download ${url}: ${response.status} ${response.statusText}`);
  }
  const buffer = Buffer.from(await response.arrayBuffer());
  fs.writeFileSync(destination, buffer, { mode: 0o755 });
}

async function installCloudflaredLocal() {
  const asset = cloudflaredReleaseAsset();
  const installPath = localCloudflaredPath();
  const binDir = path.dirname(installPath);
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'tunnel-cloudflared-'));
  const url = `https://github.com/cloudflare/cloudflared/releases/latest/download/${asset.file}`;

  fs.mkdirSync(binDir, { recursive: true, mode: 0o700 });
  console.error(`Installing cloudflared to ${installPath}`);

  try {
    if (asset.archive) {
      const archivePath = path.join(tmpRoot, asset.file);
      const extractDir = path.join(tmpRoot, 'extract');
      fs.mkdirSync(extractDir, { recursive: true });
      await downloadFile(url, archivePath);
      const tar = spawnSync('tar', ['-xzf', archivePath, '-C', extractDir], {
        encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], shell: false
      });
      if (tar.status !== 0) {
        throw new Error(`Failed to extract ${asset.file}: ${tar.stderr || tar.stdout || `exit ${tar.status}`}`);
      }
      const extracted = findFileByName(extractDir, 'cloudflared');
      if (!extracted) throw new Error(`Could not find cloudflared in ${asset.file}`);
      fs.copyFileSync(extracted, installPath);
    } else {
      const tmpBinary = path.join(tmpRoot, cloudflaredBinName());
      await downloadFile(url, tmpBinary);
      fs.copyFileSync(tmpBinary, installPath);
    }
    if (process.platform !== 'win32') fs.chmodSync(installPath, 0o755);
    verifyCloudflared(installPath);
    console.error('cloudflared installed successfully.');
    return installPath;
  } finally {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  }
}

async function resolveCloudflared(args, preferLocal = false) {
  const explicit = args.cloudflared ?? process.env.CLOUDFLARED_BIN ?? '';
  if (explicit) {
    const resolved = isPathLike(explicit) ? resolveExecutablePath(explicit) : explicit;
    if (commandAvailable(resolved)) { verifyCloudflared(resolved); return resolved; }
    throw new Error(`cloudflared not found at --cloudflared ${explicit}`);
  }

  if (!preferLocal && commandExists('cloudflared')) {
    try { verifyCloudflared('cloudflared'); return 'cloudflared'; } catch {}
  }

  const localPath = localCloudflaredPath();
  if (executableFileExists(localPath)) {
    try { verifyCloudflared(localPath); return localPath; } catch {}
  }

  return installCloudflaredLocal();
}

// ─── Ngrok resolution ───────────────────────────────────────────────────────

function verifyNgrok(binaryPath) {
  const result = spawnSync(binaryPath, ['version'], { stdio: 'ignore', shell: false, timeout: 15000 });
  if (result.status !== 0) {
    throw new Error(`ngrok at ${binaryPath} failed version.`);
  }
}

function resolveNgrok() {
  const explicit = process.env.NGROK_BIN || '';
  if (explicit) {
    const resolved = isPathLike(explicit) ? resolveExecutablePath(explicit) : explicit;
    if (commandAvailable(resolved)) { verifyNgrok(resolved); return resolved; }
    throw new Error(`ngrok not found at NGROK_BIN=${explicit}`);
  }
  if (commandExists('ngrok')) { verifyNgrok('ngrok'); return 'ngrok'; }
  throw new Error('ngrok not found on PATH. Install from https://ngrok.com/download');
}

function ngrokConfigPath(args) {
  const cp = args.ngrokConfig || process.env.NGROK_CONFIG || '';
  return cp ? path.resolve(expandHome(cp)) : '';
}

// ─── PublicBase helper ──────────────────────────────────────────────────────

function publicBaseFromHostname(hostname) {
  const raw = hostname.includes('://') ? hostname : `https://${hostname}`;
  const url = new URL(raw);
  url.pathname = url.pathname.replace(/\/+$/, '');
  url.search = '';
  url.hash = '';
  return url.toString().replace(/\/$/, '');
}

// ─── Main ───────────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs(process.argv);

  if (args.mode === 'quick') {
    const cloudflaredPath = await resolveCloudflared(args);
    console.error(`Starting Cloudflare quick tunnel for ${args.local}...`);
    const child = spawnLogged('cloudflared', cloudflaredPath, ['tunnel', '--url', args.local]);
    const publicUrl = (await waitForCloudflareUrl(child)).replace(/\/+$/, '');
    const healthUrl = args.health;
    const qsPath = args.healthPath ? (args.healthPath.startsWith('/') ? args.healthPath : `/${args.healthPath}`) : '/mcp';
    console.error(`Waiting for health at ${healthUrl}...`);
    await waitForHealth(healthUrl, args.timeout);
    console.log(`${publicUrl}${qsPath}`);
    await waitForProcessExit(child);
    return;
  }

  if (args.mode === 'cloudflare') {
    const cloudflaredPath = await resolveCloudflared(args);
    const cloudflaredArgs = ['tunnel'];

    if (args.config) {
      cloudflaredArgs.push('--config', path.resolve(expandHome(args.config)), 'run');
      if (args.name) cloudflaredArgs.push(args.name);
    } else if (args.token) {
      cloudflaredArgs.push('run', '--token', args.token);
    } else if (args.tokenFile) {
      cloudflaredArgs.push('run', '--token-file', path.resolve(expandHome(args.tokenFile)));
    } else if (args.name) {
      cloudflaredArgs.push('run', args.name);
    } else {
      throw new Error('cloudflare mode requires one of: --config, --token, --token-file, --name');
    }

    console.error('Starting Cloudflare named tunnel...');
    const child = spawnLogged('cloudflared', cloudflaredPath, cloudflaredArgs);
    const healthUrl = args.health;
    console.error(`Waiting for health at ${healthUrl}...`);
    try {
      await waitForHealth(healthUrl, args.timeout);
    } catch (error) {
      const tail = typeof child.logTail === 'function' ? child.logTail() : '';
      const msg = error instanceof Error ? error.message : String(error);
      throw new Error(`${msg}${tail ? `\n\ncloudflared output:\n${tail}` : ''}`);
    }
    if (args.hostname) {
      const base = publicBaseFromHostname(args.hostname);
      const path = args.healthPath ? (args.healthPath.startsWith('/') ? args.healthPath : `/${args.healthPath}`) : '/mcp';
      console.log(`${base}${path}`);
    } else {
      console.log(healthUrl);
    }
    await waitForProcessExit(child);
    return;
  }

  if (args.mode === 'ngrok') {
    if (!args.hostname) throw new Error('ngrok mode requires --hostname');
    const ngrokPath = resolveNgrok();
    const publicBase = publicBaseFromHostname(args.hostname);
    const ngrokArgs = ['http', args.local, '--url', publicBase];
    const configPath = ngrokConfigPath(args);
    if (configPath) ngrokArgs.push('--config', configPath);

    console.error(`Starting ngrok tunnel for ${publicBase}...`);
    const child = spawnLogged('ngrok', ngrokPath, ngrokArgs);
    const healthUrl = args.health;
    const ngrokQsPath = args.healthPath ? (args.healthPath.startsWith('/') ? args.healthPath : `/${args.healthPath}`) : '/mcp';
    console.error(`Waiting for health at ${healthUrl}...`);
    await waitForHealth(healthUrl, args.timeout);
    console.log(`${publicBase}${ngrokQsPath}`);
    await waitForProcessExit(child);
    return;
  }
}

main().catch((error) => {
  cleanupChildren();
  const msg = error instanceof Error ? error.message : String(error);
  console.error(`Error: ${msg}`);
  if (process.env.TUNNEL_DEBUG === '1' && error instanceof Error && error.stack) {
    console.error(error.stack);
  }
  process.exit(1);
});
