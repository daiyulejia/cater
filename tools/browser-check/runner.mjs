import { constants } from 'node:fs';
import { lstat, open, realpath } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { Script } from 'node:vm';
import { setTimeout as delay } from 'node:timers/promises';

const PLAN_NAME = '.cater-browser-check.json';
const SCREENSHOT_NAME = '.cater-browser-screenshot.png';
const ORIGIN = 'http://cater-artifacts.invalid';
const MAX_PLAN = 65_536;
const MAX_FILE = 16 * 1024 * 1024;
const MAX_TOTAL = 32 * 1024 * 1024;
const STEP_TIMEOUT = 3_000;
const RUN_TIMEOUT = 60_000;
const OBSERVATION_TIMEOUT = 1_000;
const OBSERVATION_CHARS = 1_200;
const ACTIONS = {
  click: { selector: 500 },
  click_at: { selector: 500, x: null, y: null },
  press: { key: 50 },
  fill: { selector: 500, value: 2000 },
  wait: { ms: null },
  assert_visible: { selector: 500 },
  assert_text: { selector: 500, text: 2000 },
  evaluate: { expression: 4000 },
  assert_js: { expression: 4000 },
};
const MIME = {
  '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.ico': 'image/x-icon', '.woff': 'font/woff', '.woff2': 'font/woff2',
  '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.mp4': 'video/mp4',
  '.wasm': 'application/wasm',
};
// CSP also covers URL schemes that Playwright's HTTP routing cannot intercept.
const CSP = [
  "sandbox allow-scripts allow-same-origin",
  "default-src 'self'", "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
  "style-src 'self' 'unsafe-inline'", "img-src 'self' data: blob:",
  "media-src 'self' data: blob:", "font-src 'self' data:",
  "connect-src 'self' http: https: ws: wss:",
  "worker-src 'none'", "child-src 'none'", "frame-src 'none'", "object-src 'none'",
  "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'",
].join('; ');

function requireThat(condition, message) {
  if (!condition) throw new Error(message);
}

function exactKeys(value, keys, where) {
  requireThat(value !== null && typeof value === 'object' && !Array.isArray(value), `${where}: expected object`);
  const actual = Object.keys(value);
  requireThat(actual.length === keys.length && keys.every(key => Object.hasOwn(value, key)), `${where}: unexpected or missing keys`);
}

function textField(value, limit, where, allowEmpty = false) {
  requireThat(typeof value === 'string' && value.isWellFormed() && [...value].length <= limit
    && (allowEmpty || value.trim().length > 0), `${where}: invalid string or length`);
}

export function safeBasename(value) {
  return typeof value === 'string' && value.isWellFormed() && value.length > 0
    && Buffer.byteLength(value) <= 180 && value === value.trim()
    && !value.startsWith('.') && !value.endsWith('.')
    && !/[\x00-\x1f\x7f\\/:*?"<>|%#]/u.test(value)
    && !/^(con|prn|aux|nul|com[1-9\u00b9\u00b2\u00b3]|lpt[1-9\u00b9\u00b2\u00b3])(?:\.|$)/i.test(value)
    && path.basename(value) === value;
}

export function validatePlan(plan) {
  exactKeys(plan, ['entrypoint', 'files', 'steps'], 'plan');
  requireThat(Array.isArray(plan.files) && plan.files.length >= 1 && plan.files.length <= 128, 'files: expected 1..128 basenames');
  requireThat(plan.files.every(safeBasename), 'files: unsafe basename');
  const folded = plan.files.map(name => name.normalize('NFC').toLowerCase());
  requireThat(new Set(folded).size === folded.length, 'files: duplicate basename');
  requireThat(safeBasename(plan.entrypoint) && /\.html?$/i.test(plan.entrypoint)
    && plan.files.includes(plan.entrypoint), 'entrypoint: expected allowlisted HTML basename');
  requireThat(Array.isArray(plan.steps) && plan.steps.length >= 1 && plan.steps.length <= 128, 'steps: expected 1..128 steps');
  for (const [index, step] of plan.steps.entries()) {
    const where = `step ${index + 1}`;
    requireThat(step && typeof step.action === 'string' && Object.hasOwn(ACTIONS, step.action), `${where}: unknown action`);
    const fields = ACTIONS[step.action];
    const optional = step.action === 'assert_js' && Object.hasOwn(step, 'observe') ? ['observe'] : [];
    exactKeys(step, ['action', 'label', ...Object.keys(fields), ...optional], where);
    textField(step.label, 100, `${where}.label`);
    requireThat(!/[\x00-\x1f\x7f]/u.test(step.label), `${where}.label: control characters are forbidden`);
    for (const [key, limit] of Object.entries(fields)) {
      if (key === 'ms') {
        requireThat(Number.isInteger(step.ms) && step.ms >= 0 && step.ms <= 2000, `${where}.ms: expected integer 0..2000`);
      } else if (key === 'x' || key === 'y') {
        requireThat(typeof step[key] === 'number' && Number.isFinite(step[key]) && step[key] >= 0 && step[key] <= 16384,
          `${where}.${key}: expected finite coordinate 0..16384`);
      } else {
        textField(step[key], limit, `${where}.${key}`, key === 'value' || (key === 'key' && step.key === ' '));
      }
    }
    if (step.action === 'evaluate' || step.action === 'assert_js') {
      // Parse only. Never run model expressions in the Node host.
      try { new Script(step.expression); }
      catch { throw new Error(`${where}: browser expression has invalid JavaScript syntax`); }
    }
    if (optional.length) {
      textField(step.observe, 4000, `${where}.observe`);
    }
  }
  requireThat(plan.steps.some(step => step.action.startsWith('assert_')), 'steps: at least one assertion is required');
  return plan;
}

async function readRegularFile(root, name, limit) {
  const filename = path.join(root, name);
  const before = await lstat(filename);
  requireThat(before.isFile() && !before.isSymbolicLink() && before.nlink === 1, `${name}: expected regular file, no symlink or hardlink`);
  requireThat(await realpath(filename) === filename, `${name}: resolved path escapes exact copied file`);
  requireThat(before.size <= limit, `${name}: file exceeds byte limit`);
  const handle = await open(filename, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const opened = await handle.stat();
    requireThat(opened.isFile() && opened.nlink === 1 && opened.ino === before.ino
      && opened.dev === before.dev && opened.size === before.size, `${name}: file changed during validation`);
    const bytes = Buffer.alloc(Math.min(limit + 1, opened.size + 1));
    let length = 0;
    while (length < bytes.length) {
      const result = await handle.read(bytes, length, bytes.length - length, length);
      if (result.bytesRead === 0) break;
      length += result.bytesRead;
    }
    const after = await lstat(filename);
    requireThat(!after.isSymbolicLink() && after.ino === opened.ino && after.dev === opened.dev
      && after.size === opened.size && after.mtimeMs === opened.mtimeMs
      && await realpath(filename) === filename && length === opened.size,
    `${name}: file changed during read`);
    return bytes.subarray(0, length);
  } finally {
    await handle.close();
  }
}

async function loadInputs(argv) {
  requireThat(argv.length === 2 && argv[0] === '--plan' && argv[1] === PLAN_NAME,
    `usage: node <absolute runner.mjs> --plan ${PLAN_NAME}`);
  const root = await realpath(process.cwd());
  const bytes = await readRegularFile(root, PLAN_NAME, MAX_PLAN);
  const plan = validatePlan(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)));
  const files = new Map();
  let total = 0;
  for (const name of plan.files) {
    const body = await readRegularFile(root, name, Math.min(MAX_FILE, MAX_TOTAL - total));
    total += body.length;
    files.set(`${ORIGIN}/${encodeURIComponent(name)}`, { body, name });
  }
  // Never follow or overwrite a pre-existing screenshot, including a link.
  try {
    await lstat(path.join(root, SCREENSHOT_NAME));
    throw new Error(`${SCREENSHOT_NAME}: output already exists; use a fresh artifact copy`);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  return { root, plan, files, planSha256: createHash('sha256').update(bytes).digest('hex') };
}

function clip(value, maxBytes) {
  const clean = String(value).slice(0, maxBytes * 2).replace(/[\x00-\x1f\x7f]/g, ' ');
  let result = '';
  let bytes = 0;
  for (const char of clean) {
    bytes += Buffer.byteLength(char);
    if (bytes > maxBytes - 3) return `${result}...`;
    result += char;
  }
  return result;
}

function errorText(error) {
  return clip(error instanceof Error ? error.message : error, 240);
}

function append(report, field, value, limit = 6) {
  report.counts[field] += 1;
  if (report[field].length < limit) report[field].push(value);
}

function stepSummary(step, index) {
  const result = { index: index + 1, action: step.action, label: clip(step.label, 120), status: 'not_run' };
  const detail = step.expression ?? (step.action === 'assert_text'
    ? `${step.selector} contains ${JSON.stringify(step.text)}` : step.selector ?? step.key ?? `${step.ms}ms`);
  result.check = clip(detail, 240);
  if (step.action.startsWith('assert_')) result.expected = true;
  return result;
}

export function serializeReport(report) {
  const out = structuredClone(report);
  out.truncated = false;
  const encode = () => `${JSON.stringify(out)}\n`;
  if (Buffer.byteLength(encode()) <= 7500) return encode();
  // Remove recoverable plan metadata before shortening any actual observation.
  out.steps = out.steps.map(step => Object.fromEntries(
    ['index', 'status', 'actual', 'error', 'observed'].filter(key => Object.hasOwn(step, key))
      .map(key => [key, step[key]])));
  out.steps_encoding = 'plan_index_v1';
  out.details_compacted = true;
  if (Buffer.byteLength(encode()) <= 7500) return encode();
  out.truncated = true;
  for (const cap of [120, 72, 36]) {
    for (const step of out.steps) {
      if (step.error) step.error = clip(step.error, cap);
      if (step.observed) step.observed = clip(step.observed, cap);
    }
    for (const field of ['console_messages', 'blocked_requests', 'page_errors']) {
      out[field] = out[field].slice(0, cap === 36 ? 1 : 3);
    }
    if (Buffer.byteLength(encode()) <= 7500) return encode();
  }
  // All inputs and arrays are already bounded; keep every step even at the final budget tier.
  for (const step of out.steps) {
    delete step.observed;
  }
  return encode();
}

async function bounded(operation, milliseconds, label) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label}: timeout after ${milliseconds}ms`)), milliseconds); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function executeStep(page, step, result) {
  switch (step.action) {
    case 'click': await page.locator(step.selector).click(); break;
    case 'click_at': {
      const target = page.locator(step.selector);
      await target.waitFor({ state: 'visible' });
      const box = await target.boundingBox();
      requireThat(box && step.x < box.width && step.y < box.height, 'click_at: position is outside target');
      await target.click({ position: { x: step.x, y: step.y } });
      break;
    }
    case 'press': await page.keyboard.press(step.key); break;
    case 'fill': await page.locator(step.selector).fill(step.value); break;
    case 'wait': await delay(step.ms); break;
    case 'assert_visible':
      await page.locator(step.selector).waitFor({ state: 'visible' });
      result.actual = true;
      break;
    case 'assert_text': {
      const locator = page.locator(step.selector);
      await locator.waitFor({ state: 'attached' });
      const actual = await locator.textContent();
      result.actual = typeof actual === 'string' && actual.includes(step.text);
      result.observed = clip(actual ?? '', 120);
      requireThat(result.actual, 'assert_text: text did not contain expected substring');
      break;
    }
    case 'evaluate':
    case 'assert_js': {
      // Playwright serializes this callback into Chromium; eval never runs in Node.
      const actual = await page.evaluate(async ({ source, assertion }) => {
        let value = (0, eval)(source);
        if (typeof value === 'function') value = value();
        value = await value;
        return assertion ? value === true : null;
      }, { source: step.expression, assertion: step.action === 'assert_js' });
      if (step.action === 'assert_js') {
        result.actual = actual;
        requireThat(actual === true, 'assert_js: expression did not return exactly true');
      }
      break;
    }
  }
}

async function observeFailure(page, expression) {
  return bounded(() => page.evaluate(async ({ source, limit }) => {
    let value = (0, eval)(source);
    if (typeof value === 'function') value = value();
    value = await value;
    const encoded = typeof value === 'string' ? value : JSON.stringify(value);
    const text = encoded === undefined ? String(value) : encoded;
    const chars = Array.from(text);
    return chars.length > limit ? chars.slice(0, limit).join('') + '…（诊断已截断）' : text;
  }, { source: expression, limit: OBSERVATION_CHARS }), OBSERVATION_TIMEOUT, 'failure observation');
}

async function checkArtifacts(browser, inputs, report) {
  const { root, plan, files } = inputs;
  const context = await browser.newContext({
    acceptDownloads: false, serviceWorkers: 'block', permissions: [],
    viewport: { width: 1024, height: 768 }, deviceScaleFactor: 1,
    ignoreHTTPSErrors: false, bypassCSP: false,
  });
  context.setDefaultTimeout(STEP_TIMEOUT);
  context.setDefaultNavigationTimeout(5_000);
  let page;
  let initialRequest = true;
  let navigationViolation = false;
  let closing = false;
  const entryUrl = `${ORIGIN}/${encodeURIComponent(plan.entrypoint)}`;
  const block = (kind, url) => append(report, 'blocked_requests', { kind, url: clip(url, 180) }, 8);
  const routeFailure = error => {
    if (!closing) append(report, 'page_errors', `interception: ${errorText(error)}`);
  };
  context.on('weberror', event => append(report, 'page_errors', errorText(event.error())));
  context.on('console', message => {
    if (message.type() === 'warning' || message.type() === 'error') {
      append(report, 'console_messages', { type: message.type(), text: clip(message.text(), 180) });
    }
  });
  context.on('page', opened => {
    if (page && opened !== page) {
      block('popup', opened.url());
      void opened.close().catch(routeFailure);
    }
  });
  await context.routeWebSocket(/.*/, async socket => {
    block('websocket', socket.url());
    await socket.close({ code: 1008, reason: 'Network disabled' }).catch(routeFailure);
  });
  await context.route(/.*/, async route => {
    try {
      const request = route.request();
      const url = request.url();
      const file = files.get(url);
      let allow = request.method() === 'GET' && Boolean(file);
      if (request.isNavigationRequest()) {
        allow = allow && initialRequest && url === entryUrl && request.frame() === page?.mainFrame();
        if (allow) initialRequest = false;
      }
      if (!allow) {
        block(request.isNavigationRequest() ? 'navigation' : 'request', url);
        await route.abort('blockedbyclient');
        return;
      }
      await route.fulfill({ status: 200, body: file.body, headers: {
        'content-type': MIME[path.extname(file.name).toLowerCase()] ?? 'application/octet-stream',
        'content-security-policy': CSP, 'x-content-type-options': 'nosniff',
        'cache-control': 'no-store', 'referrer-policy': 'no-referrer',
        'x-dns-prefetch-control': 'off',
        'permissions-policy': 'camera=(), microphone=(), geolocation=(), usb=(), serial=(), bluetooth=()',
      } });
    } catch (error) {
      routeFailure(error);
      await route.abort('blockedbyclient').catch(() => {});
    }
  });
  page = await context.newPage();
  page.on('dialog', dialog => { void dialog.dismiss().catch(routeFailure); });
  page.on('download', download => {
    block('download', download.url());
    void download.cancel().catch(routeFailure);
  });
  page.on('crash', () => append(report, 'page_errors', 'Chromium page crashed'));
  page.on('framenavigated', frame => {
    if (frame === page.mainFrame() && frame.url().split('#')[0] !== entryUrl) {
      navigationViolation = true;
      block('navigation', frame.url());
      void page.close().catch(routeFailure);
    }
  });
  try {
    await page.goto(entryUrl, { waitUntil: 'load' });
    for (const [index, step] of plan.steps.entries()) {
      const result = report.steps[index];
      result.status = 'running';
      try {
        await bounded(() => executeStep(page, step, result), STEP_TIMEOUT + 250, `step ${index + 1}`);
        requireThat(!navigationViolation && !page.isClosed(), 'page left the permitted artifact document');
        result.status = 'passed';
      } catch (error) {
        result.status = 'failed';
        result.error = errorText(error);
        if (step.action === 'assert_js' && Object.hasOwn(step, 'observe')) {
          try { result.observed = await observeFailure(page, step.observe); }
          catch (observationError) {
            result.error += `\n状态采集未完成：${errorText(observationError)}`;
          }
        }
        break;
      }
    }
    if (!page.isClosed()) {
      try {
        const screenshot = await page.screenshot({ type: 'png', fullPage: false, timeout: 3_000 });
        const handle = await open(path.join(root, SCREENSHOT_NAME), 'wx', 0o600);
        try { await handle.writeFile(screenshot); } finally { await handle.close(); }
        report.screenshot = { path: SCREENSHOT_NAME, sha256: createHash('sha256').update(screenshot).digest('hex'), size: screenshot.length };
      } catch (error) {
        report.screenshot_error = errorText(error);
      }
    }
    requireThat(report.steps.every(step => step.status === 'passed'), 'one or more steps failed or were not run');
    requireThat(!navigationViolation && !page.isClosed(), 'page left the permitted artifact document');
    requireThat(report.counts.page_errors === 0, 'page reported uncaught JavaScript errors');
    requireThat(report.screenshot !== null, 'screenshot could not be recorded');
  } finally {
    closing = true;
    await bounded(() => context.close(), 3_000, 'context close');
  }
}

export async function run(argv = process.argv.slice(2)) {
  const report = {
    outcome: 'failed', phase: 'validation', steps: [], page_errors: [], blocked_requests: [], console_messages: [],
    counts: { page_errors: 0, blocked_requests: 0, console_messages: 0 }, screenshot: null,
  };
  let browser;
  let exitCode = 1;
  let inputs;
  try {
    inputs = await loadInputs(argv);
    report.plan_sha256 = inputs.planSha256;
    report.steps = inputs.plan.steps.map(stepSummary);
    report.phase = 'launch';
    // Debug and Selenium environment switches must not redirect this local-only adapter.
    for (const name of Object.keys(process.env)) {
      if (name === 'DEBUG' || name === 'PWDEBUG' || name.startsWith('SELENIUM_') || name.startsWith('PW_TEST_')) delete process.env[name];
    }
    const { chromium } = await import('playwright');
    browser = await chromium.launch({
      headless: true, chromiumSandbox: true, timeout: 15_000,
      proxy: { server: 'http://cater-network-denied.invalid:9', bypass: '<-loopback>' },
      args: ['--host-resolver-rules=MAP * ~NOTFOUND', '--disable-quic',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
    });
    report.phase = 'execution';
    await bounded(() => checkArtifacts(browser, inputs, report), RUN_TIMEOUT, 'browser check');
    report.outcome = 'passed';
    exitCode = 0;
  } catch (error) {
    report.error = errorText(error);
    if (report.phase === 'launch') {
      report.outcome = 'unavailable';
      exitCode = 2;
    }
    for (const step of report.steps) {
      if (step.status === 'running') {
        step.status = 'failed';
        step.error = report.error;
      }
    }
  } finally {
    if (browser) {
      try { await bounded(() => browser.close(), 3_000, 'browser close'); }
      catch (error) {
        report.outcome = 'failed';
        report.error = errorText(error);
        exitCode = 1;
      }
    }
  }
  // Closing the context can deliver a final page error after the last assertion.
  if (report.counts.page_errors && exitCode === 0) {
    report.outcome = 'failed';
    report.error = 'page reported uncaught JavaScript errors';
    exitCode = 1;
  }
  return { report, exitCode };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { report, exitCode } = await run();
  process.stdout.write(serializeReport(report));
  process.exitCode = exitCode;
}
