import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFile } from 'node:child_process';
import { mkdtemp, writeFile, readFile, rm, copyFile, symlink, link, mkdir, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { validatePlan, safeBasename } from '../runner.mjs';

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
  for (const [name, content] of Object.entries(extras)) await writeFile(path.join(directory, name), content);
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
    planFor([]), planFor(Array.from({ length: 33 }, () => assertion())),
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
  assert.equal(report.truncated, true);
  assert.ok(report.steps.every(step => step.action === 'assert_js' && step.label.length > 0));
});
