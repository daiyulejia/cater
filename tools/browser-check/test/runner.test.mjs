import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFile } from 'node:child_process';
import { mkdtemp, writeFile, readFile, rm, copyFile, symlink, link, mkdir, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { validatePlan, safeBasename, safeRelativePath, serializeReport } from '../runner.mjs';

const RUNNER = fileURLToPath(new URL('../runner.mjs', import.meta.url));
const PLAN = '.cater-browser-check.json';
const SHOT = '.cater-browser-screenshot.png';
const html = `<!doctype html><html><head><meta charset="utf-8"><title>Fixture</title></head>
<body><button id="start">Start</button><input id="name"><output id="score">0</output>
<script>
globalThis.score = 0;
document.querySelector('#start').onclick = () => { score++; document.querySelector('#score').textContent = String(score); };
document.querySelector('#name').onkeydown = event => { if (event.key === 'Enter') document.querySelector('#score').textContent = event.target.value; };
</script></body></html>`;
const assertion = (expression = 'true', label = '检查实际结果') => ({ action: 'assert_js', label, expression });
const planFor = (steps = [assertion()], files = ['index.html']) => ({ entrypoint: 'index.html', files, steps });
const relativePlan = (files = ['pages/index.html'], steps = [assertion()]) => ({
  files_layout: 'relative_paths_v1', entrypoint: files[0], files, steps,
});

test('relative path policy checks original and NFKC segments with exact bounds', () => {
  const valid = ['pages/index.html', '资源 目录/a b.png', "assets/it's+ok.js", 'a/'.repeat(31) + 'b',
    'a'.repeat(240), '图'.repeat(80), '😀'.repeat(60), 'a'.repeat(239) + '/x',
    [...Array(8).fill('a'.repeat(240)), 'b'.repeat(120)].join('/')];
  for (const name of valid) assert.equal(safeRelativePath(name), true, name);
  const invalid = ['', '.', '..', '/a', 'a/', 'a//b', 'a/./b', 'a/../b', 'a\\b', 'C:/a', 'c:a',
    '//server/a', '.hidden/a', 'a/.hidden', 'a/x.', 'a/x ', 'a/ x', 'a/\tbad',
    'a/%2e%2e/b', 'a/x#y', 'a/x%y', 'a/x~1', 'a/x~9.txt', 'a/\ud800',
    'a/'.repeat(32) + 'b', 'a'.repeat(241), '图'.repeat(81), '😀'.repeat(61),
    [...Array(8).fill('a'.repeat(240)), 'b'.repeat(121)].join('/')];
  for (const c of ['\0', '\x7f', '\u0085', '\u200d', '\ufeff', '\ue000', '\uffff', '\u0378',
    '\\', ':', '*', '?', '"', '<', '>', '|', '%', '#', '\uff0f', '\uff3c', '\uff1a']) invalid.push(`a/x${c}y`);
  for (const device of ['CON', 'prn', 'AUX', 'nul', 'CONIN$', 'conout$',
    ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
    ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`), 'COM¹', 'COM²', 'LPT³', 'ＣＯＮ', 'ＣＯＭ１']) {
    invalid.push(`a/${device}`, `${device}/x`, `a/${device}.txt`, `a/${device} .txt`);
  }
  invalid.push('a/．hidden', 'a/x．', 'a/\u00a0x', 'a/x\u00a0', 'a/x～１');
  for (const name of invalid) assert.equal(safeRelativePath(name), false, JSON.stringify(name));
  for (const value of [null, undefined, [], {}, 12]) assert.equal(safeRelativePath(value), false);
});

test('relative plan exact schema, entry, limits and directory/leaf aliases', () => {
  const good = relativePlan(['pages/index.html', 'pages/app.js', 'assets/x.png']);
  assert.equal(validatePlan(good), good);
  const invalid = [
    { ...good, files_layout: 'relative_paths_v2' }, { ...good, extra: true },
    { ...good, entrypoint: 'pages/INDEX.html' }, { ...good, entrypoint: 'pages/app.js' },
    { ...good, files: [] }, { ...good, files: Array(129).fill('a') },
    { ...good, steps: [] }, { ...good, steps: Array.from({ length: 129 }, () => assertion()) },
    { ...good, steps: [{ action: 'wait', label: 'wait', ms: 2001 }, assertion()] },
  ];
  for (const files of [
    ['pages/index.html', 'pages/INDEX.HTML'], ['pages/index.html', 'Pages/app.js'],
    ['pages/index.html', 'ｐａｇｅｓ/app.js'], ['pages/index.html', 'pages'],
    ['pages/index.html', 'pages/app', 'pages/app/x.js'],
    ['pages/index.html', 'pages/app/x.js', 'pages/app'],
    ['pages/index.html', 'é/a.js', 'e\u0301/b.js'],
    ['pages/index.html', 'assets/K.js', 'assets/K.js'],
    ['pages/index.html', '.hidden/a.js'], ['pages/index.html', '%2e%2e/a.js'],
    ['pages/index.html', 'CON/a.js'],
  ]) invalid.push({ ...good, files });
  for (const plan of invalid) assert.throws(() => validatePlan(plan));
  assert.doesNotThrow(() => validatePlan(relativePlan(['index.html',
    ...Array.from({ length: 127 }, (_, i) => `assets/file${i}.js`)], Array.from({ length: 128 }, () => assertion()))));
  // Preserve legacy NFC-only aliases and its original basename policy, even where v1 is stricter.
  for (const name of ['CONIN$.txt', 'x~1.txt', 'x\u200d.txt']) {
    assert.equal(safeBasename(name), true);
    assert.doesNotThrow(() => validatePlan(planFor(undefined, ['index.html', name])));
    assert.equal(safeRelativePath(name), false);
  }
  assert.doesNotThrow(() => validatePlan(planFor(undefined, ['index.html', 'K.txt', 'Ｋ.txt'])));
  assert.throws(() => validatePlan({ ...good, files_layout: undefined }));
  assert.throws(() => validatePlan({ ...good, files_layout: null }));
  assert.throws(() => validatePlan({ ...good, files_layout: false }));
});

test('128 observed assertions fit the host capture without losing results', () => {
  const report = { outcome: 'passed', plan_sha256: 'a'.repeat(64), counts: { page_errors: 0 },
    console_messages: [], blocked_requests: [], page_errors: [],
    steps: Array.from({ length: 128 }, (_, index) => ({ index: index + 1, action: 'assert_js',
      label: '检'.repeat(100), check: 'x'.repeat(1000), status: 'passed', actual: true, expected: true })) };
  const serialized = serializeReport(report);
  const result = JSON.parse(serialized);
  assert.ok(Buffer.byteLength(serialized) <= 7500);
  assert.equal(result.steps_encoding, 'plan_index_v1');
  assert.equal(result.truncated, false);
  assert.equal(result.steps.length, 128);
  assert.ok(result.steps.every((step, index) => step.index === index + 1 && step.status === 'passed' && step.actual === true));
  assert.equal(report.steps[0].label.length, 100);
});

test('failed long plans retain the complete failure and observed text before clipping', () => {
  const report = { outcome: 'failed', plan_sha256: 'b'.repeat(64), counts: { page_errors: 0 },
    console_messages: [], blocked_requests: [], page_errors: [],
    steps: Array.from({ length: 65 }, (_, index) => ({ index: index + 1, action: 'assert_text',
      label: '检'.repeat(100), check: 'x'.repeat(240), status: index === 3 ? 'failed' : 'not_run',
      ...(index === 3 ? { actual: false, error: 'The exact expected result was not observed.', observed: 'Actual text' } : {}) })) };
  const serialized = serializeReport(report);
  const result = JSON.parse(serialized);
  assert.ok(Buffer.byteLength(serialized) <= 7500);
  assert.equal(result.steps_encoding, 'plan_index_v1');
  assert.equal(result.truncated, false);
  assert.equal(result.steps.length, 65);
  assert.equal(result.steps[3].error, report.steps[3].error);
  assert.equal(result.steps[3].observed, 'Actual text');
  assert.equal(result.steps[3].actual, false);
});

async function temporary(t) {
  const directory = await mkdtemp(path.join(tmpdir(), 'cater-browser-test-'));
  t.after(async () => {
    // Remove only the exact directory allocated by this test, never a computed ancestor.
    assert.equal(path.dirname(directory), path.resolve(tmpdir()));
    assert.ok(path.basename(directory).startsWith('cater-browser-test-'));
    await rm(directory, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  });
  return directory;
}

async function fixture(t, plan = planFor(), body = html, extras = {}) {
  const directory = await temporary(t);
  await writeFile(path.join(directory, 'index.html'), body);
  for (const [name, content] of Object.entries(extras)) {
    await mkdir(path.dirname(path.join(directory, name)), { recursive: true });
    await writeFile(path.join(directory, name), content);
  }
  await writeFile(path.join(directory, PLAN), JSON.stringify(plan));
  return directory;
}

function invoke(directory, { runner = RUNNER, env = {}, args = ['--plan', PLAN] } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [runner, ...args], {
      cwd: directory, env: { ...process.env, ...env }, windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'], detached: process.platform !== 'win32',
    });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      if (process.platform === 'win32') execFile('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
      else process.kill(-child.pid, 'SIGKILL');
      reject(new Error('runner exceeded test process-tree deadline'));
    }, 88_000);
    child.stdout.setEncoding('utf8').on('data', chunk => { stdout += chunk; });
    child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
    child.on('error', error => { clearTimeout(timer); reject(error); });
    child.on('close', code => {
      clearTimeout(timer);
      try {
        assert.ok(Buffer.byteLength(stdout) <= 7500, `oversized report: ${Buffer.byteLength(stdout)}`);
        assert.equal(stdout.trim().split('\n').length, 1, `not one JSON line: ${stdout}`);
        const report = JSON.parse(stdout);
        assert.ok(['passed', 'failed', 'unavailable'].includes(report.outcome));
        resolve({ code, report, stdout, stderr });
      } catch (error) { reject(new Error(`${error.message}\nstderr: ${stderr}`)); }
    });
  });
}

test('portable URL punctuation loads exactly selected flat and nested dependencies', async t => {
  for (const relative of [false, true]) {
    for (const name of ['app+v1.js', 'app@v1.js']) {
      await t.test(`${relative ? 'relative' : 'flat'} ${name}`, async t => {
        const entrypoint = relative ? 'site/index+@.html' : 'index.html';
        const dependency = relative ? `site/${name}` : name;
        const plan = { entrypoint, files: [entrypoint, dependency], steps: [assertion('globalThis.loaded === 7')],
          ...(relative ? { files_layout: 'relative_paths_v1' } : {}) };
        const page = `<!doctype html><script src="${name}"></script>`;
        const directory = await fixture(t, plan, page, {
          ...(relative ? { [entrypoint]: page } : {}), [dependency]: 'globalThis.loaded=7;',
        });
        const result = await invoke(directory);
        assert.equal(result.code, 0, JSON.stringify(result));
        assert.equal(result.report.outcome, 'passed');
        assert.equal(result.report.counts.blocked_requests, 0);
      });
    }
  }
});

const nestedFiles = ['页面 dir/index page.html', '页面 dir/js/app.js', '页面 dir/js/value.js',
  '页面 dir/css/style sheet.css', '页面 dir/images/pixel image.png'];
const nestedBody = `<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="css/style%20sheet.css">
<button id="start">Start</button><output id="score">0</output>
<img id="pixel" src="images/pixel%20image.png">
<script type="module" src="js/app.js"></script>`;
const nestedAssets = {
  [nestedFiles[0]]: nestedBody,
  [nestedFiles[1]]: `import { value } from './value.js';
    document.querySelector('#start').onclick = () => document.querySelector('#score').textContent = String(value);`,
  [nestedFiles[2]]: 'export const value = 7;',
  [nestedFiles[3]]: '#start { color: rgb(12, 34, 56); background-image: url("../images/pixel%20image.png"); }',
  [nestedFiles[4]]: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=', 'base64'),
};
const nestedSteps = [
  { action: 'click', label: 'nested module interaction', selector: '#start' },
  { action: 'assert_text', label: 'imported value', selector: '#score', text: '7' },
  assertion(`getComputedStyle(document.querySelector('#start')).color === 'rgb(12, 34, 56)'`),
  assertion(`() => { const img = document.querySelector('#pixel'); return img.complete && img.naturalWidth === 1 && img.naturalHeight === 1; }`),
];

test('real Chromium: v1 nested HTML, module imports, CSS and PNG visibly affect the page', async t => {
  const plan = relativePlan(nestedFiles, nestedSteps);
  const directory = await fixture(t, plan, html, nestedAssets);
  // Unselected entries must never be inspected or served.
  await symlink(directory, path.join(directory, 'unselected-link'), process.platform === 'win32' ? 'junction' : 'dir');
  const { code, report } = await invoke(directory);
  assert.equal(code, 0, JSON.stringify(report));
  assert.ok(report.steps.every(step => step.status === 'passed'));
  assert.equal(report.counts.blocked_requests, 0);
  assert.equal(report.plan_sha256, createHash('sha256').update(await readFile(path.join(directory, PLAN))).digest('hex'));
  const png = await readFile(path.join(directory, SHOT));
  assert.equal(report.screenshot.sha256, createHash('sha256').update(png).digest('hex'));
});

test('real Chromium: missing allowlist dependencies fail the corresponding actual observation', async t => {
  for (const [missing, failedIndex] of [[nestedFiles[1], 1], [nestedFiles[2], 1], [nestedFiles[3], 2], [nestedFiles[4], 3]]) {
    await t.test(missing, async t => {
      const plan = relativePlan(nestedFiles.filter(name => name !== missing), nestedSteps);
      const directory = await fixture(t, plan, html, nestedAssets);
      const { code, report } = await invoke(directory);
      assert.equal(code, 1, JSON.stringify(report));
      assert.equal(report.phase, 'execution');
      assert.equal(report.steps[failedIndex].status, 'failed', JSON.stringify(report));
      assert.ok(report.blocked_requests.some(request => request.url.endsWith(missing.split('/').map(encodeURIComponent).join('/'))));
    });
  }
});

test('real Chromium: v1 outbound, encoded traversal, query and unselected nested resources stay blocked', async t => {
  const urls = ['https://external.invalid/no', 'http://192.0.2.1/no',
    '/页面%20dir/images/%2e%2e/%2e%2e/not-allowlisted.txt', '/..%2fnot-allowlisted.txt',
    '/页面%20dir/js%2fapp.js', '/页面%20dir/images/pixel%20image.png?query=1',
    '/页面%20dir/.hidden.js'];
  const steps = [...nestedSteps, assertion(`Promise.all(${JSON.stringify(urls)}.map(url => fetch(url).then(() => false, () => true))).then(values => values.every(Boolean))`)];
  const directory = await fixture(t, relativePlan(nestedFiles, steps), html,
    { ...nestedAssets, 'not-allowlisted.txt': 'unselected', '页面 dir/.hidden.js': 'unselected' });
  const { code, report } = await invoke(directory);
  assert.equal(code, 0, JSON.stringify(report));
  assert.ok(report.counts.blocked_requests >= urls.length);
});

test('v1 refuses missing files, parent links, leaf links, hardlinks and directories before launch', async t => {
  const outside = await temporary(t);
  await writeFile(path.join(outside, 'index.html'), html);
  for (const kind of ['missing', 'parent-junction', 'nested-parent-junction', 'file-symlink', 'hardlink', 'directory']) {
    await t.test(kind, async t => {
      const name = kind === 'nested-parent-junction' ? 'pages/deeper/index.html' : 'pages/index.html';
      const directory = await fixture(t, relativePlan([name]));
      if (kind === 'parent-junction') {
        await symlink(outside, path.join(directory, 'pages'), process.platform === 'win32' ? 'junction' : 'dir');
      } else {
        await mkdir(path.join(directory, 'pages'));
        if (kind === 'nested-parent-junction') {
          await symlink(outside, path.join(directory, 'pages/deeper'), process.platform === 'win32' ? 'junction' : 'dir');
        } else if (kind === 'file-symlink') {
          try { await symlink(path.join(outside, 'index.html'), path.join(directory, name), 'file'); }
          catch (error) {
            if (error.code === 'EPERM') { t.skip('Windows account cannot create file symlinks'); return; }
            throw error;
          }
        } else if (kind === 'hardlink') await link(path.join(outside, 'index.html'), path.join(directory, name));
        else if (kind === 'directory') await mkdir(path.join(directory, name));
      }
      const { code, report } = await invoke(directory, { env: { PLAYWRIGHT_BROWSERS_PATH: path.join(directory, 'missing-browser') } });
      assert.equal(code, 1, JSON.stringify(report));
      assert.equal(report.phase, 'validation');
      assert.equal(report.screenshot, null);
    });
  }
});

test('v1 invalid path plans fail validation even when Chromium is unavailable', async t => {
  for (const files of [
    ['index.html', '.hidden/a.js'], ['index.html', '%2e%2e/a.js'],
    ['index.html', 'CONIN$/a.js'], ['index.html', 'LPT²/a.js'],
    ['index.html', 'assets/a.js', 'ａｓｓｅｔｓ/b.js'],
    ['index.html', 'assets/a.js', 'ASSETS/b.js'],
  ]) {
    const directory = await fixture(t, relativePlan(files));
    const { code, report } = await invoke(directory, { env: { PLAYWRIGHT_BROWSERS_PATH: path.join(directory, 'missing-browser') } });
    assert.equal(code, 1);
    assert.equal(report.phase, 'validation');
    assert.equal(report.screenshot, null);
  }
});

test('v1 preserves the realpath cwd contract for a root junction', async t => {
  const target = await fixture(t, relativePlan(), html, { 'pages/index.html': html });
  const container = await temporary(t);
  const alias = path.join(container, 'root-alias');
  await symlink(target, alias, process.platform === 'win32' ? 'junction' : 'dir');
  const { code, report } = await invoke(alias);
  assert.equal(code, 0, JSON.stringify(report));
});

test('v1 rejects file and parent changes between validation, open and completed read', async t => {
  for (const mutation of ['before-open', 'during-read', 'parent-during-read', 'hardlink-during-read']) {
    await t.test(mutation, async t => {
      const directory = await fixture(t, relativePlan(), html, { 'pages/index.html': html });
      // Wrap the standard fs API in an isolated child to schedule real disk changes exactly
      // at the read boundary; no polling race or browser dependency is involved.
      const source = `
        import fs from 'node:fs/promises';
        import path from 'node:path';
        import { syncBuiltinESMExports } from 'node:module';
        const originalOpen = fs.open;
        let changed = false;
        const mutate = async filename => {
          if (changed) return;
          changed = true;
          if (${JSON.stringify(mutation)} === 'parent-during-read') {
            await fs.writeFile(path.join(path.dirname(filename), 'new-sibling'), 'changed');
          } else if (${JSON.stringify(mutation)} === 'hardlink-during-read') {
            await fs.link(filename, path.join(path.dirname(filename), 'new-link'));
          } else {
            const stat = await fs.stat(filename);
            const bytes = await fs.readFile(filename);
            bytes[0] ^= 1;
            await fs.writeFile(filename, bytes);
            await fs.utimes(filename, stat.atime, stat.mtime);
          }
        };
        fs.open = async (filename, ...args) => {
          if (!filename.endsWith(path.join('pages', 'index.html'))) return originalOpen(filename, ...args);
          if (${JSON.stringify(mutation)} === 'before-open') await mutate(filename);
          const handle = await originalOpen(filename, ...args);
          const originalRead = handle.read.bind(handle);
          handle.read = async (...readArgs) => {
            const result = await originalRead(...readArgs);
            if (${JSON.stringify(mutation)} !== 'before-open') await mutate(filename);
            return result;
          };
          return handle;
        };
        syncBuiltinESMExports();
        const { run, serializeReport } = await import(${JSON.stringify(pathToFileURL(RUNNER).href)});
        const { report, exitCode } = await run(['--plan', ${JSON.stringify(PLAN)}]);
        if (!changed) throw new Error('mutation hook did not execute');
        process.stdout.write(serializeReport(report));
        process.exitCode = exitCode;
      `;
      const { code, report } = await invoke(directory, { runner: '--input-type=module', args: ['--eval', source],
        env: { PLAYWRIGHT_BROWSERS_PATH: path.join(directory, 'missing-browser') } });
      assert.equal(code, 1, JSON.stringify(report));
      assert.equal(report.phase, 'validation');
      assert.match(report.error, /changed during/);
    });
  }
});

test('real Chromium: all action types, page expressions, assets, Unicode names, screenshot receipt', async t => {
  const steps = [
    { action: 'assert_visible', label: '按钮可见', selector: '#start' },
    { action: 'click', label: '点击开始', selector: '#start' },
    { action: 'assert_text', label: '分数变更', selector: '#score', text: '1' },
    { action: 'fill', label: '输入名称', selector: '#name', value: 'Ada' },
    { action: 'press', label: '按下回车', key: 'Enter' },
    { action: 'assert_text', label: '提交名称', selector: '#score', text: 'Ada' },
    { action: 'evaluate', label: '设置测试状态', expression: '(() => { window.setupValue = 42; return new Uint8Array(1000000); })()' },
    { action: 'wait', label: '等待一帧', ms: 20 },
    assertion('window.setupValue === 42 && window.assetValue === 7', 'English labels are accepted'),
    assertion(`fetch('/数据 file.txt').then(r => r.text()).then(s => s === 'known fixture')`),
  ];
  const directory = await fixture(t, planFor(steps, ['index.html', 'asset.js', '数据 file.txt']),
    html.replace('</body>', '<script src="asset.js"></script></body>'),
    { 'asset.js': 'window.assetValue = 7;', '数据 file.txt': 'known fixture' });
  const { code, report } = await invoke(directory);
  assert.equal(code, 0, JSON.stringify(report));
  assert.equal(report.outcome, 'passed');
  assert.equal(report.steps.length, steps.length);
  assert.ok(report.steps.every(step => step.status === 'passed'));
  assert.equal(report.counts.page_errors, 0);
  const png = await readFile(path.join(directory, SHOT));
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.equal(png.readUInt32BE(16), 1024);
  assert.equal(png.readUInt32BE(20), 768);
  assert.equal(report.screenshot.size, png.length);
  assert.equal(report.screenshot.sha256, createHash('sha256').update(png).digest('hex'));
});

test('false assertion fails and later steps are explicitly not run', async t => {
  const directory = await fixture(t, planFor([assertion('false'), assertion('true')]));
  const { code, report } = await invoke(directory);
  assert.equal(code, 1);
  assert.equal(report.outcome, 'failed');
  assert.equal(report.steps[0].actual, false);
  assert.equal(report.steps[0].status, 'failed');
  assert.equal(report.steps[1].status, 'not_run');
  assert.ok(report.screenshot);
});

test('assert_js rejects truthy non-boolean values, including function return values', async t => {
  for (const expression of ['1', '"true"', '({})', '(() => 1)']) {
    await t.test(expression, async t => {
      const directory = await fixture(t, planFor([assertion(expression)]));
      const { code, report } = await invoke(directory);
      assert.equal(code, 1);
      assert.equal(report.steps[0].actual, false);
    });
  }
});

test('browser-only evaluation supports arrow functions, statements and persistent setup state; single-space key works', async t => {
  const steps = [
    { action: 'fill', label: '聚焦输入', selector: '#name', value: '' },
    { action: 'press', label: '空格键', key: ' ' },
    assertion(`() => document.querySelector('#name').value === ' '`),
    { action: 'evaluate', label: '多语句准备', expression: 'window.__caterCheck = { count: 1 }; window.__caterCheck.count += 2;' },
    { action: 'evaluate', label: '函数更新状态', expression: '() => { window.__caterCheck.count += 4; }' },
    assertion('async () => { await Promise.resolve(); return window.__caterCheck.count === 7; }'),
    assertion('window.__caterCheck.count += 1; window.__caterCheck.count === 8;'),
    assertion(`() => typeof process === 'undefined' && typeof require === 'undefined' && typeof Buffer === 'undefined'`),
  ];
  const directory = await fixture(t, planFor(steps));
  const { code, report } = await invoke(directory);
  assert.equal(code, 0, JSON.stringify(report));
  assert.ok(report.steps.every(step => step.status === 'passed'));
});

test('page exception fails despite a passing assertion; warnings alone do not fail', async t => {
  await t.test('uncaught exception', async t => {
    const directory = await fixture(t, planFor(), `${html}<script>throw new Error('fixture exploded');</script>`);
    const { code, report } = await invoke(directory);
    assert.equal(code, 1);
    assert.equal(report.steps[0].status, 'passed');
    assert.ok(report.page_errors.some(error => error.includes('fixture exploded')));
  });
  await t.test('console warning', async t => {
    const directory = await fixture(t, planFor(), `${html}<script>console.warn('unrelated warning');</script>`);
    const { code, report } = await invoke(directory);
    assert.equal(code, 0, JSON.stringify(report));
    assert.ok(report.console_messages.some(message => message.text.includes('unrelated warning')));
  });
});

test('network deny: HTTP, HTTPS, unlisted files, query variants, POST, traversal, WebSocket and file reads', async t => {
  const directory = await fixture(t);
  const secret = path.join(directory, 'not-allowlisted.txt');
  await writeFile(secret, 'private fixture, never served');
  const urls = [
    'http://external.invalid/no', 'https://external.invalid/no',
    'http://192.0.2.1/no', '/not-allowlisted.txt', '/index.html?query=1',
    '/%2e%2e/not-allowlisted.txt', '/..%2fnot-allowlisted.txt',
    pathToFileURL(secret).href,
  ];
  const steps = [
    assertion(`new Promise(resolve => { const socket = new WebSocket('wss://socket.invalid/blocked'); socket.onclose = () => resolve(true); socket.onerror = () => resolve(true); setTimeout(() => resolve(false), 1500); })`),
    assertion(`Promise.all(${JSON.stringify(urls)}.map(url => fetch(url).then(() => false, () => true))).then(results => results.every(Boolean))`),
    assertion(`fetch('/index.html', { method: 'POST', body: 'no' }).then(() => false, () => true)`),
  ];
  await writeFile(path.join(directory, PLAN), JSON.stringify(planFor(steps)));
  const { code, report } = await invoke(directory);
  assert.equal(code, 0, JSON.stringify(report));
  assert.ok(report.counts.blocked_requests >= 8, JSON.stringify(report));
  assert.ok(report.blocked_requests.some(request => request.kind === 'websocket'));
});

test('popups, frame content, workers and downloads are disallowed by browser policy', async t => {
  const steps = [
    assertion(`window.open('http://cater-artifacts.invalid/other.html') === null`),
    assertion(`new Promise(resolve => { const frame = document.createElement('iframe'); frame.src = 'other.html'; document.body.append(frame); setTimeout(() => { try { resolve(frame.contentDocument?.body?.textContent !== 'secret frame'); } catch { resolve(true); } }, 200); })`),
    assertion(`new Promise(resolve => { try { const worker = new Worker('worker.js'); worker.onmessage = () => resolve(false); worker.onerror = event => { event.preventDefault(); resolve(true); }; setTimeout(() => resolve(false), 1000); } catch { resolve(true); } })`),
    { action: 'click', label: '尝试下载', selector: '#download' },
    { action: 'wait', label: '等待下载事件', ms: 200 },
    assertion('true'),
  ];
  const body = `${html}<a id="download" href="data:text/plain,secret" download="forbidden.txt">Download</a>`;
  const directory = await fixture(t, planFor(steps, ['index.html', 'other.html', 'worker.js']), body,
    { 'other.html': '<body>secret frame</body>', 'worker.js': 'postMessage("unexpected");' });
  const { code, report } = await invoke(directory);
  assert.equal(code, 0, JSON.stringify(report));
  assert.deepEqual((await readdir(directory)).sort(), [PLAN, SHOT, 'index.html', 'other.html', 'worker.js'].sort());
});

test('navigation cannot load another allowlisted document or a non-network document', async t => {
  await t.test('HTTP navigation aborted', async t => {
    const steps = [
      { action: 'evaluate', label: '尝试跳转', expression: "(location.href = '/other.html')" },
      { action: 'wait', label: '等待拦截', ms: 100 },
      assertion("location.pathname === '/index.html'"),
    ];
    const directory = await fixture(t, planFor(steps, ['index.html', 'other.html']), html,
      { 'other.html': '<script>throw new Error("must never run")</script>' });
    const { report } = await invoke(directory);
    assert.ok(report.blocked_requests.some(request => request.kind === 'navigation'));
    assert.equal(report.counts.page_errors, 0);
  });
  await t.test('about navigation closes page and fails', async t => {
    const steps = [{ action: 'evaluate', label: '尝试空白跳转', expression: "(location.href = 'about:blank')" },
      { action: 'wait', label: '等待跳转事件', ms: 200 }, assertion()];
    const directory = await fixture(t, planFor(steps));
    const { code, report } = await invoke(directory);
    assert.equal(code, 1);
    assert.ok(report.blocked_requests.some(request => request.url === 'about:blank'));
  });
});

test('browser expressions cannot access Node, process env, require or host bindings', async t => {
  const steps = [
    assertion(`typeof process === 'undefined' && typeof require === 'undefined' && typeof module === 'undefined' && typeof Buffer === 'undefined'`),
    assertion(`(() => { try { return window.constructor.constructor('return process')() === null; } catch (error) { return error instanceof ReferenceError; } })()`),
    assertion(`import('node:fs').then(() => false, () => true)`),
    { action: 'evaluate', label: '仅页面状态', expression: '(() => { globalThis.browserOnly = 123; })()' },
    assertion('globalThis.browserOnly === 123'),
  ];
  const directory = await fixture(t, planFor(steps));
  const { code, report } = await invoke(directory, { env: { CATER_PRIVATE_TEST_SECRET: 'host secret' } });
  assert.equal(code, 0, JSON.stringify(report));
});

test('each invocation starts with empty browser storage', async t => {
  for (let index = 0; index < 2; index++) {
    const directory = await fixture(t, planFor([
      assertion(`localStorage.getItem('previous') === null && document.cookie === ''`),
      { action: 'evaluate', label: '写入临时存储', expression: `(() => { localStorage.setItem('previous', 'present'); document.cookie = 'previous=present'; })()` },
    ]));
    const { code, report } = await invoke(directory);
    assert.equal(code, 0, JSON.stringify(report));
  }
});

test('unresolved expressions are time bounded', async t => {
  const directory = await fixture(t, planFor([assertion('new Promise(() => {})')]));
  const started = Date.now();
  const { code, report } = await invoke(directory);
  assert.equal(code, 1);
  assert.match(report.steps[0].error, /timeout/i);
  assert.ok(Date.now() - started < 15_000);
});

test('CPU-bound browser expressions cannot hang the Node runner indefinitely', async t => {
  const directory = await fixture(t, planFor([assertion('(() => { while (true) {} })()')]));
  const started = Date.now();
  const { code, report } = await invoke(directory);
  assert.equal(code, 1);
  assert.equal(report.steps[0].status, 'failed');
  assert.match(report.steps[0].error, /timeout/i);
  assert.ok(Date.now() - started < 20_000);
});

test('missing elements and incorrect text are genuine failing assertions', async t => {
  for (const step of [
    { action: 'assert_visible', label: '不存在的元素', selector: '#missing' },
    { action: 'assert_text', label: '错误的分数', selector: '#score', text: '999' },
  ]) {
    const directory = await fixture(t, planFor([step]));
    const { code, report } = await invoke(directory);
    assert.equal(code, 1);
    assert.equal(report.steps[0].status, 'failed');
  }
});

test('strict schema rejects extra keys, unsafe names, malformed limits and no assertions', () => {
  const invalid = [
    null, [], { ...planFor(), args: ['--no-sandbox'] },
    { ...planFor(), entrypoint: 'other.html' }, { ...planFor(), files: [] },
    { ...planFor(), files: ['index.html', 'INDEX.HTML'] },
    { ...planFor(), files: ['index.html', 'e\u0301.txt', '\u00e9.txt'] },
    planFor([]), planFor(Array.from({ length: 129 }, () => assertion())),
    planFor([{ action: 'wait', label: '等待', ms: 1 }]),
    planFor([{ ...assertion(), shell: 'echo no' }]),
    planFor([{ ...assertion(), action: '__proto__' }]),
    planFor([{ ...assertion(), label: '' }]), planFor([{ ...assertion(), label: 'x'.repeat(101) }]),
    planFor([{ ...assertion(), label: 'bad\nlabel' }]), planFor([assertion('x'.repeat(4001))]),
    planFor([assertion(' ')]), planFor([assertion('\ud800')]),
    ...[-1, 2001, 1.5, '1', true].map(ms => planFor([{ action: 'wait', label: '等待', ms }, assertion()])),
    planFor([{ action: 'click', label: '点击', selector: 'x'.repeat(501) }, assertion()]),
    planFor([{ action: 'press', label: '按键', key: 'x'.repeat(51) }, assertion()]),
    planFor([{ action: 'press', label: '按键', key: '  ' }, assertion()]),
    planFor([{ action: 'fill', label: '填入', selector: '#name', value: 'x'.repeat(2001) }, assertion()]),
    planFor([{ action: 'assert_text', label: '文本', selector: '#score', text: 'x'.repeat(2001) }]),
    ...[-1, 16385, NaN, Infinity, true, '10'].map(x => planFor([{ action: 'click_at', label: '坐标', selector: '#start', x, y: 1 }, assertion()])),
    planFor([assertion('() => {')]),
  ];
  for (const plan of invalid) assert.throws(() => validatePlan(plan));
  for (const filename of ['../index.html', 'a/b', 'a\\b', 'C:foo', '.hidden', 'CON.txt', 'COM1.html',
    'COM\u00b9.txt', 'x.', 'x ', ' x', 'x%20.txt', 'x#y', 'x\x7f', 'x\0', 'x'.repeat(181)]) {
    assert.equal(safeBasename(filename), false, filename);
    assert.throws(() => validatePlan({ ...planFor(), files: ['index.html', filename] }));
  }
  assert.doesNotThrow(() => validatePlan(planFor([assertion('true', 'English label')])));
  assert.doesNotThrow(() => validatePlan(planFor([{ action: 'fill', label: '清空', selector: '#name', value: '' }, assertion()])));
});

test('coordinate clicks use real pointer positions and reject outside targets', async t => {
  const body = '<!doctype html><div id="board" style="width:300px;height:100px"></div><output id="out"></output>'
    + '<script>document.querySelector("#board").onclick=e=>document.querySelector("#out").textContent=String(Math.floor(e.offsetX/100));</script>';
  const steps = [
    { action: 'click_at', label: '第一格', selector: '#board', x: 50, y: 50 },
    { action: 'assert_text', label: '第一格结果', selector: '#out', text: '0' },
    { action: 'click_at', label: '第三格', selector: '#board', x: 250, y: 50 },
    { action: 'assert_text', label: '第三格结果', selector: '#out', text: '2' },
  ];
  const good = await invoke(await fixture(t, planFor(steps), body));
  assert.equal(good.code, 0);
  assert.ok(good.report.steps.every(step => step.status === 'passed'));
  const bad = await invoke(await fixture(t, planFor([{ ...steps[0], x: 300 }, assertion()]), body));
  assert.equal(bad.code, 1);
  assert.match(bad.report.steps[0].error, /outside target/);
});

test('invalid test syntax is a validation failure before Chromium or page actions', async t => {
  const directory = await fixture(t, planFor([{ action: 'evaluate', label: '错误脚本', expression: '() => {' }, assertion()]));
  const { code, report } = await invoke(directory);
  assert.equal(code, 1);
  assert.equal(report.phase, 'validation');
  assert.match(report.error, /invalid JavaScript syntax/);
});

test('validation failures remain failed/1 without launching Chromium', async t => {
  const cases = [
    ['no assertions', planFor([{ action: 'wait', label: '等待', ms: 0 }])],
    ['traversal', { ...planFor(), files: ['index.html', '../outside.txt'] }],
    ['unknown key', { ...planFor(), arbitraryCode: 'process.exit(0)' }],
  ];
  for (const [name, plan] of cases) {
    await t.test(name, async t => {
      const directory = await fixture(t, plan);
      const { code, report } = await invoke(directory, { env: { PLAYWRIGHT_BROWSERS_PATH: path.join(directory, 'missing') } });
      assert.equal(code, 1);
      assert.equal(report.phase, 'validation');
      assert.equal(report.outcome, 'failed');
      assert.equal(report.screenshot, null);
    });
  }
});

test('plan bytes and CLI arguments are bounded and exact', async t => {
  for (const content of [' '.repeat(65_537), '{broken JSON', Buffer.from([0xff, 0xfe])]) {
    const directory = await fixture(t);
    await writeFile(path.join(directory, PLAN), content);
    const { code, report } = await invoke(directory);
    assert.equal(code, 1);
    assert.equal(report.phase, 'validation');
  }
  const directory = await fixture(t);
  const { code, report } = await invoke(directory, { args: ['--plan', '../elsewhere.json'] });
  assert.equal(code, 1);
  assert.match(report.error, /usage/);
});

test('symlink/junction/hardlink artifacts and output links cannot escape the fixture', async t => {
  const outside = await temporary(t);
  const secret = path.join(outside, 'secret.html');
  await writeFile(secret, '<script>throw new Error("outside")</script>');
  await t.test('file symlink', async t => {
    const directory = await fixture(t);
    await rm(path.join(directory, 'index.html'));
    try { await symlink(secret, path.join(directory, 'index.html'), 'file'); }
    catch (error) {
      if (error.code === 'EPERM') { t.skip('Windows account cannot create file symlinks; junction coverage still runs'); return; }
      throw error;
    }
    const { code, report } = await invoke(directory);
    assert.equal(code, 1);
    assert.equal(report.phase, 'validation');
  });
  await t.test('junction', async t => {
    const directory = await fixture(t, planFor([assertion()], ['index.html', 'outside.html']));
    await symlink(outside, path.join(directory, 'outside.html'), process.platform === 'win32' ? 'junction' : 'dir');
    const { code, report } = await invoke(directory);
    assert.equal(code, 1);
    assert.equal(report.phase, 'validation');
  });
  await t.test('hardlink', async t => {
    const directory = await fixture(t);
    await rm(path.join(directory, 'index.html'));
    await link(secret, path.join(directory, 'index.html'));
    const { code, report } = await invoke(directory);
    assert.equal(code, 1);
    assert.equal(report.phase, 'validation');
  });
  await t.test('screenshot junction', async t => {
    const directory = await fixture(t);
    await symlink(outside, path.join(directory, SHOT), process.platform === 'win32' ? 'junction' : 'dir');
    const { code, report } = await invoke(directory);
    assert.equal(code, 1);
    assert.equal(report.phase, 'validation');
    assert.equal(await readFile(secret, 'utf8'), '<script>throw new Error("outside")</script>');
  });
});

test('dependency absence and Chromium absence are unavailable/2, not assertion failures', async t => {
  await t.test('missing dependency', async t => {
    const directory = await fixture(t);
    const isolated = path.join(directory, 'isolated-runner.mjs');
    await copyFile(RUNNER, isolated);
    const { code, report } = await invoke(directory, { runner: isolated });
    assert.equal(code, 2);
    assert.equal(report.outcome, 'unavailable');
    assert.equal(report.phase, 'launch');
    assert.ok(report.steps.every(step => step.status === 'not_run'));
  });
  await t.test('missing Chromium', async t => {
    const directory = await fixture(t);
    const missing = path.join(directory, 'no-browsers');
    await mkdir(missing);
    const { code, report } = await invoke(directory, { env: { PLAYWRIGHT_BROWSERS_PATH: missing } });
    assert.equal(code, 2);
    assert.equal(report.outcome, 'unavailable');
    assert.equal(report.phase, 'launch');
  });
});

test('large labels, expressions, errors and log storms keep all steps within 7500 UTF-8 bytes', async t => {
  const steps = Array.from({ length: 32 }, (_, i) => assertion(`${' '.repeat(1900)}${i < 31 ? 'true' : 'false'}`, '检'.repeat(100)));
  const plan = planFor(steps);
  // Keep the input below 64 KiB while still forcing report compaction.
  for (const step of plan.steps) step.expression = step.expression.slice(1000);
  const body = `${html}<script>for (let i = 0; i < 40; i++) { console.warn('警'.repeat(2000)); fetch('http://external.invalid/' + i).catch(() => {}); }</script>`;
  const directory = await fixture(t, plan, body);
  const { code, report, stdout } = await invoke(directory);
  assert.equal(code, 1);
  assert.equal(report.steps.length, 32);
  assert.equal(report.steps[31].status, 'failed');
  assert.ok(report.counts.console_messages >= 40);
  assert.ok(report.counts.blocked_requests >= 40);
  assert.ok(Buffer.byteLength(stdout) <= 7500);
  assert.equal(report.steps_encoding, 'plan_index_v1');
  assert.equal(report.details_compacted, true);
  assert.ok(report.steps.every((step, index) => step.index === index + 1));
  assert.equal(report.steps[31].actual, false);
  assert.match(report.steps[31].error, /did not return exactly true/);
});

test('optional failure observations preserve strict plan validation without running host code', () => {
  const legacy = planFor();
  assert.equal(validatePlan(legacy), legacy);
  const valid = planFor([{ ...assertion('false'), observe: '() => ({count: 0})' }]);
  assert.equal(validatePlan(valid), valid);
  for (const observe of ['', '  ', null, 1, false, [], {}, 'x'.repeat(4001)]) {
    assert.throws(() => validatePlan(planFor([{ ...assertion('false'), observe }])));
  }
  assert.throws(() => validatePlan(planFor([{ action: 'evaluate', label: '准备', expression: '0', observe: '0' }, assertion()])));
  assert.throws(() => validatePlan(planFor([{ ...assertion('false'), observation: '0' }])));
  assert.doesNotThrow(() => validatePlan(planFor([{ ...assertion('false'), observe: 'process.exit(17)' }])));
  assert.doesNotThrow(() => validatePlan(planFor([{ ...assertion('false'), observe: '() => {' }])));
});

test('real Chromium: failed assertion collects actual page state and never becomes passed', async t => {
  const directory = await fixture(t, planFor([
    { action: 'click', label: '点击一次', selector: '#start' },
    { ...assertion('score === 2'), observe: '() => ({score, text: document.querySelector("#score").textContent})' },
    assertion('true', '失败后不应执行'),
  ]));
  const { code, report } = await invoke(directory);
  assert.equal(code, 1);
  assert.equal(report.outcome, 'failed');
  assert.equal(report.steps[1].actual, false);
  assert.equal(report.steps[1].status, 'failed');
  assert.deepEqual(JSON.parse(report.steps[1].observed), { score: 1, text: '1' });
  assert.match(report.steps[1].error, /did not return exactly true/);
  assert.equal(report.steps[2].status, 'not_run');
});

test('real Chromium: passing assertion never executes the optional observation', async t => {
  const directory = await fixture(t, planFor([
    { ...assertion('true'), observe: '() => { globalThis.unwantedObservation = true; throw new Error("not allowed on pass"); }' },
    assertion('globalThis.unwantedObservation === undefined'),
  ]));
  const { code, report } = await invoke(directory);
  assert.equal(code, 0);
  assert.equal(report.outcome, 'passed');
  assert.equal(Object.hasOwn(report.steps[0], 'observed'), false);
  assert.equal(Object.hasOwn(report.steps[0], 'error'), false);
});

test('real Chromium: diagnostic errors and timeout do not replace the primary failure', async t => {
  for (const [name, observe] of [
    ['throw', '() => { throw new Error("diagnostic failed"); }'],
    ['cycle', '() => { const value = {}; value.self = value; return value; }'],
    ['bigint', '() => 1n'],
    ['timeout', '() => new Promise(() => {})'],
  ]) await t.test(name, async () => {
    const directory = await fixture(t, planFor([{ ...assertion('false'), observe }]));
    const { code, report } = await invoke(directory);
    assert.equal(code, 1);
    assert.equal(report.outcome, 'failed');
    assert.equal(report.steps[0].actual, false);
    assert.match(report.steps[0].error, /^assert_js: expression did not return exactly true/);
    assert.match(report.steps[0].error, /状态采集未完成/);
    assert.equal(Object.hasOwn(report.steps[0], 'observed'), false);
  });
});

test('real Chromium: thrown assertion can report state; observations stay page-only and bounded', async t => {
  for (const [name, observe, expected] of [
    ['page-only', '() => typeof process + ":" + typeof require', 'undefined:undefined'],
    ['undefined', '() => undefined', 'undefined'],
    ['async', 'async () => ({score, available: true})', '{"score":0,"available":true}'],
    ['unicode', '() => "😀".repeat(200000)', '😀'.repeat(1200) + '…（诊断已截断）'],
    ['mutating', '() => { globalThis.score = 100; return true; }', 'true'],
  ]) await t.test(name, async () => {
    const directory = await fixture(t, planFor([{ ...assertion('missingFixtureName === 1'), observe }]));
    const { code, report, stdout } = await invoke(directory);
    assert.equal(code, 1);
    assert.equal(report.outcome, 'failed');
    assert.equal(report.steps[0].status, 'failed');
    assert.equal(Object.hasOwn(report.steps[0], 'actual'), false);
    assert.match(report.steps[0].error, /ReferenceError/);
    assert.equal(report.steps[0].observed, expected);
    assert.ok(Buffer.byteLength(stdout) <= 7500);
  });
});

test('real Chromium: invalid optional observation syntax does not block the primary assertion', async t => {
  const directory = await fixture(t, planFor([{ ...assertion('false'), observe: '() => {' }]));
  const { code, report } = await invoke(directory);
  assert.equal(code, 1);
  assert.equal(report.outcome, 'failed');
  assert.equal(report.phase, 'execution');
  assert.equal(report.steps[0].actual, false);
  assert.match(report.steps[0].error, /^assert_js: expression did not return exactly true/);
  assert.match(report.steps[0].error, /状态采集未完成/);
  assert.match(report.steps[0].error, /SyntaxError/);
  const passing = await fixture(t, planFor([{ ...assertion('true'), observe: '() => {' }]));
  assert.equal((await invoke(passing)).code, 0);
});

test('observation expressions participate in the exact plan hash', async t => {
  const plan = planFor([{ ...assertion('false'), observe: '() => ({score})' }]);
  const directory = await fixture(t, plan);
  const bytes = await readFile(path.join(directory, PLAN));
  const { report } = await invoke(directory);
  assert.equal(report.plan_sha256, createHash('sha256').update(bytes).digest('hex'));
  assert.equal(report.steps[0].observed, '{"score":0}');
});
