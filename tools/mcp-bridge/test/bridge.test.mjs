import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, access } from 'node:fs/promises';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { canonicalJSON, schemaHash, LIMITS } from '../bridge.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const ROOT_KEYS = new Set(['status', 'complete', 'outcome', 'operation', 'tools', 'total', 'offset', 'next_offset', 'has_more',
  'code', 'message', 'note', 'mcp_result', 'representation_complete', 'unsupported_content_types', 'result_bytes',
  'result_sha256', 'result_excerpt', 'schema_sha256']);
const sha = text => createHash('sha256').update(text).digest('hex');

async function fixture(t, mode = 'normal') {
  const dir = await mkdtemp(join(root, '.test-tmp-'));
  const logPath = join(dir, 'server.ndjson');
  const statePath = join(dir, 'state.txt');
  const proc = spawn(process.execPath, [join(root, 'bridge.mjs')], {
    cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, FIXTURE_PARENT_SECRET: 'PARENT_SECRET_SENTINEL', HOME: 'PARENT_HOME_SENTINEL' },
  });
  const exited = once(proc, 'exit');
  let stderr = '';
  proc.stderr.on('data', data => { stderr += data; });
  const lines = [];
  const waiters = [];
  const reader = createInterface({ input: proc.stdout });
  reader.on('line', line => {
    assert.ok(Buffer.byteLength(line) + 1 <= LIMITS.packet);
    const packet = JSON.parse(line);
    if (packet.result) assert.ok(Object.keys(packet.result).every(key => ROOT_KEYS.has(key)));
    const next = waiters.shift();
    if (next) next(packet); else lines.push(packet);
  });
  const config = {
    type: 'complete', server: { command: [process.execPath, join(root, 'test', 'fixture-server.mjs'), mode, logPath, statePath],
      cwd: dir, env: { FIXTURE_EXPLICIT: 'EXPLICIT_VALUE',
        ...(process.platform === 'win32' ? { SYSTEMROOT: process.env.SystemRoot ?? process.env.SYSTEMROOT } : {}) } },
    allowed_tools: ['write_note', 'read_note'], timeout_ms: 5000,
  };
  let seq = 0;
  const read = async () => {
    let timer;
    try {
      return await Promise.race([
        lines.length ? Promise.resolve(lines.shift()) : new Promise(resolve => waiters.push(resolve)),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Fixture bridge reply deadline')), 12000); }),
      ]);
    } finally { clearTimeout(timer); }
  };
  const send = overrides => {
    const request = { ...config, id: `opaque-${++seq}`, action: 'list', ...overrides };
    proc.stdin.write(`${JSON.stringify(request)}\n`);
    return request;
  };
  const exchange = async overrides => {
    const request = send(overrides);
    const packet = await read();
    assert.equal(packet.id, request.id);
    return packet;
  };
  const events = async () => {
    try { return (await readFile(logPath, 'utf8')).trim().split('\n').filter(Boolean).map(line => JSON.parse(line)); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  };
  const close = async () => {
    if (proc.exitCode === null) proc.stdin.end();
    let timer;
    try {
      const result = await Promise.race([exited, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Bridge close deadline')), 7000); })]);
      assert.equal(result[0], 0);
    } finally { clearTimeout(timer); }
    assert.equal(stderr, '');
  };
  t.after(async () => {
    if (proc.exitCode === null) proc.kill();
    await exited;
    reader.close();
    for (const event of await events()) {
      if (event.event === 'spawn') {
        try { process.kill(event.pid, 0); process.kill(event.pid); } catch { /* fixture already reaped */ }
      }
    }
    assert.equal(stderr, '');
    await rm(dir, { recursive: true, force: true });
  });
  const list = async () => {
    const response = await exchange({ action: 'list' });
    assert.equal(response.ok, true);
    assert.equal(response.result.status, 'ok');
    return response.result;
  };
  const call = (schema, args = { text: 'local-write' }, overrides = {}) => exchange({
    action: 'call', tool_name: 'write_note', schema_sha256: schema,
    arguments: args, invocation_id: `event:${seq + 1}`, ...overrides,
  });
  return { dir, proc, config, send, read, exchange, events, close, list, call, statePath };
}

test('canonical hash recursively sorts keys, preserves arrays and JSON special keys', () => {
  const value = JSON.parse('{"b":[{"z":2,"a":1}],"__proto__":{"2":"b","10":"a"},"a":0}');
  assert.equal(canonicalJSON(value), '{"__proto__":{"10":"a","2":"b"},"a":0,"b":[{"a":1,"z":2}]}');
  const tool = { name: 'test', inputSchema: { type: 'object', properties: { z: { type: 'integer' }, a: { type: 'string' } } } };
  assert.equal(schemaHash(tool), sha('{"inputSchema":{"properties":{"a":{"type":"string"},"z":{"type":"integer"}},"type":"object"},"name":"test"}'));
  assert.equal(schemaHash({ ...tool, title: 'Different' }), schemaHash(tool));
  assert.notEqual(schemaHash({ ...tool, name: 'other' }), schemaHash(tool));
});

test('SDK initialization, allowlist pagination, persistent connection, explicit env, actual write/read, EOF close', async t => {
  const f = await fixture(t);
  const page = (await f.exchange({ offset: 0, limit: 1 })).result;
  assert.equal(page.total, 2);
  assert.equal(page.tools[0].name, 'write_note');
  assert.equal(page.has_more, true);
  assert.equal(page.next_offset, 1);
  assert.deepEqual(Object.keys(page.tools[0]).sort(), ['description', 'input_schema', 'name', 'schema_sha256', 'title']);
  const tail = (await f.exchange({ offset: 1, limit: 1 })).result;
  assert.equal(tail.tools[0].name, 'read_note');
  assert.equal(tail.next_offset, 2);
  const reply = await f.call(page.tools[0].schema_sha256);
  assert.equal(reply.result.outcome, 'returned');
  assert.equal(reply.result.complete, true);
  assert.equal(reply.result.representation_complete, true);
  assert.deepEqual(reply.result.mcp_result.structuredContent, { written: true });
  const original = { content: [{ type: 'text', text: 'written' }], structuredContent: { written: true } };
  assert.equal(reply.result.result_bytes, Buffer.byteLength(JSON.stringify(original)));
  assert.equal(reply.result.result_sha256, sha(JSON.stringify(original)));
  assert.equal(await readFile(f.statePath, 'utf8'), 'local-write\n');
  const read = await f.call(tail.tools[0].schema_sha256, {}, { tool_name: 'read_note' });
  assert.equal(read.result.mcp_result.content[0].text, 'local-write\n');
  const empty = (await f.exchange({ offset: 999, limit: 1 })).result;
  assert.deepEqual(empty.tools, []);
  assert.equal(empty.has_more, false);
  await f.close();
  const events = await f.events();
  const starts = events.filter(e => e.event === 'spawn');
  assert.equal(starts.length, 1);
  assert.equal(starts[0].cwd, f.dir);
  assert.equal(starts[0].env.FIXTURE_EXPLICIT, 'EXPLICIT_VALUE');
  assert.equal(starts[0].env.FIXTURE_PARENT_SECRET, undefined);
  assert.equal(starts[0].env.HOME, undefined);
  assert.equal(starts[0].env.PATH, undefined);
  assert.deepEqual(Object.keys(starts[0].env).map(key => key.toUpperCase()).sort(), Object.keys(f.config.server.env).map(key => key.toUpperCase()).sort());
  assert.deepEqual(events.find(e => e.event === 'initialized').capabilities, {});
  assert.equal(events.filter(e => e.event === 'call')[0].meta['cater/invocation_id'], 'event:3');
  assert.ok(events.some(e => e.event === 'eof'));
  assert.ok(events.some(e => e.event === 'exit'));
  assert.ok(!JSON.stringify([page, tail, reply]).includes('FIXTURE_PRIVATE'));
});

test('unauthorized call refuses before any spawn', async t => {
  const f = await fixture(t);
  const reply = await f.call('0'.repeat(64), {}, { tool_name: 'hidden_tool' });
  assert.equal(reply.result.code, 'MCP_TOOL_NOT_ALLOWED');
  assert.equal(reply.result.outcome, 'not_started');
  assert.deepEqual(await f.events(), []);
  await f.close();
});

test('actual strict input validation rejects wrong types, extras, formats, 2020-12 tuples with zero calls', async t => {
  const f = await fixture(t);
  const schema = (await f.list()).tools[0].schema_sha256;
  for (const args of [{}, { text: 5 }, { text: 'x', extra: true }, { text: 'x', email: 'not-email' },
    { text: 'x', pair: ['ok', 'coercion-forbidden'] }, { text: 'x', pair: ['ok', 2, 3] }]) {
    const response = await f.call(schema, args);
    assert.equal(response.result.code, 'MCP_ARGUMENTS_INVALID');
    assert.equal(response.result.outcome, 'not_started');
  }
  assert.equal((await f.call('0'.repeat(64))).result.code, 'MCP_SCHEMA_CHANGED');
  assert.equal((await f.call(schema, {}, { tool_name: 'missing', allowed_tools: ['write_note', 'read_note', 'missing'] })).result.code, 'MCP_CONFIGURATION_CHANGED');
  assert.equal((await f.events()).filter(e => e.event === 'call').length, 0);
  await assert.rejects(access(f.statePath));
  const valid = await f.call(schema, { text: 'valid', email: 'fixture@example.invalid', pair: ['yes', 2] });
  assert.equal(valid.result.outcome, 'returned');
  await f.close();
});

for (const [mode, code] of [['changed', 'MCP_SCHEMA_CHANGED'], ['unknown_schema', 'MCP_SCHEMA_UNSUPPORTED'], ['tasks', 'MCP_TASKS_UNSUPPORTED']]) {
  test(`${mode} rejects before tools/call`, async t => {
    const f = await fixture(t, mode);
    const schema = (await f.list()).tools[0].schema_sha256;
    const response = await f.call(schema);
    assert.equal(response.result.code, code);
    assert.equal(response.result.outcome, 'not_started');
    assert.equal((await f.events()).filter(e => e.event === 'call').length, 0);
    await f.close();
  });
}

test('missing allowlisted tool and unsupported output schema refuse with zero tool calls', async t => {
  for (const mode of ['normal', 'unknown_output']) {
    await t.test(mode, async t => {
      const f = await fixture(t, mode);
      if (mode === 'normal') f.config.allowed_tools.push('missing');
      const schema = (await f.list()).tools[0].schema_sha256;
      const response = await f.call(schema, { text: 'never' }, mode === 'normal' ? { tool_name: 'missing' } : {});
      assert.equal(response.result.code, mode === 'normal' ? 'MCP_TOOL_NOT_FOUND' : 'MCP_SCHEMA_UNSUPPORTED');
      assert.equal(response.result.outcome, 'not_started');
      assert.equal((await f.events()).filter(e => e.event === 'call').length, 0);
      await f.close();
    });
  }
});

test('input and output schemas sharing a remote $id do not collide', async t => {
  const f = await fixture(t, 'schema_id');
  const schema = (await f.list()).tools[0].schema_sha256;
  assert.equal((await f.call(schema)).result.outcome, 'returned');
  await f.close();
});

test('normal isError is a complete terminal even after a write', async t => {
  const f = await fixture(t);
  const schema = (await f.list()).tools[0].schema_sha256;
  const response = await f.call(schema, { text: 'changed-before-error', behavior: 'error' });
  assert.equal(response.result.status, 'error');
  assert.equal(response.result.outcome, 'tool_error');
  assert.equal(response.result.complete, true);
  assert.equal(response.result.mcp_result.isError, true);
  assert.equal(await readFile(f.statePath, 'utf8'), 'changed-before-error\n');
  assert.equal((await f.call(schema)).result.outcome, 'returned');
  await f.close();
});

for (const behavior of ['crash', 'timeout', 'bad_output', 'protocol_error', 'invalid_terminal', 'oversize']) {
  test(`write then ${behavior} is unknown, private-safe, closes and never reconnects/replays`, async t => {
    const f = await fixture(t);
    const schema = (await f.list()).tools[0].schema_sha256;
    const response = await f.call(schema, { text: 'once', behavior }, { timeout_ms: behavior === 'timeout' ? 500 : 5000 });
    assert.equal(response.result.outcome, 'effects_unknown');
    assert.equal(response.result.complete, false);
    assert.equal(response.result.code, 'MCP_EFFECTS_UNKNOWN');
    assert.ok(!JSON.stringify(response).includes('FIXTURE_PRIVATE'));
    assert.equal(await readFile(f.statePath, 'utf8'), 'once\n');
    const retry = await f.call(schema, { text: 'forbidden-reconnect' });
    assert.equal(retry.result.outcome, 'not_started');
    assert.equal((await f.events()).filter(e => e.event === 'call').length, 1);
    assert.equal((await f.events()).filter(e => e.event === 'spawn').length, 1);
    await f.close();
  });
}

for (const behavior of ['large', 'escape', 'media', 'metadata', 'structured_large', 'large_error', 'media_error']) {
  test(`${behavior} is a known terminal with bounded, explicitly incomplete representation`, async t => {
    const f = await fixture(t);
    const schema = (await f.list()).tools[0].schema_sha256;
    const response = await f.call(schema, { text: 'bounded', behavior });
    assert.equal(response.result.outcome, behavior.endsWith('_error') ? 'tool_error' : 'returned');
    assert.equal(response.result.complete, true);
    assert.equal(response.result.representation_complete, false);
    assert.equal(response.result.code, 'MCP_REPRESENTATION_INCOMPLETE');
    assert.ok(Buffer.byteLength(JSON.stringify(response.result.mcp_result)) <= LIMITS.body);
    assert.match(response.result.result_sha256, /^[a-f0-9]{64}$/);
    assert.ok(response.result.result_bytes > Buffer.byteLength(JSON.stringify(response.result.mcp_result)));
    if (behavior === 'media') {
      assert.deepEqual(response.result.unsupported_content_types, ['audio', 'image', 'resource', 'resource_link']);
      assert.deepEqual(response.result.mcp_result.content, [{ type: 'text', text: 'Media summary' }]);
    }
    if (behavior === 'large') {
      const raw = { content: [{ type: 'text', text: '\u4e2d'.repeat(90000) }], structuredContent: { written: true } };
      assert.equal(response.result.result_bytes, Buffer.byteLength(JSON.stringify(raw)));
      assert.equal(response.result.result_sha256, sha(JSON.stringify(raw)));
      assert.ok(!response.result.mcp_result.content[0].text.includes('\ufffd'));
    }
    assert.ok(!JSON.stringify(response).includes('FIXTURE_PRIVATE_RESULT_METADATA'));
    assert.equal((await f.call(schema)).result.outcome, 'returned');
    await f.close();
  });
}

for (const mode of ['structured', 'deep']) {
  test(`${mode} valid structured result remains terminal when representation is omitted`, async t => {
    const f = await fixture(t, mode);
    const schema = (await f.list()).tools.find(tool => tool.name === 'read_note').schema_sha256;
    const response = await f.call(schema, {}, { tool_name: 'read_note' });
    assert.equal(response.result.outcome, 'returned');
    assert.equal(response.result.complete, true);
    assert.equal(response.result.representation_complete, false);
    assert.equal(response.result.mcp_result.structuredContent, undefined);
    let raw;
    if (mode === 'structured') raw = { content: [{ type: 'text', text: 'JSON summary' }], structuredContent: { blob: 'S'.repeat(200000) } };
    else {
      let nested = { leaf: 'value' };
      for (let i = 0; i < 100; i++) nested = { child: nested };
      raw = { content: [{ type: 'text', text: 'Deep JSON summary' }], structuredContent: nested };
    }
    assert.equal(response.result.result_bytes, Buffer.byteLength(JSON.stringify(raw)));
    assert.equal(response.result.result_sha256, sha(JSON.stringify(raw)));
    await f.close();
  });
}

test('large allowlisted definitions paginate below Python result budget without dropping tools', async t => {
  const f = await fixture(t, 'large_directory');
  f.config.allowed_tools = Array.from({ length: 8 }, (_, i) => `allowed_${i}`);
  let offset = 0;
  const names = [];
  do {
    const packet = await f.exchange({ offset, limit: 20 });
    const result = packet.result;
    assert.equal(result.status, 'ok');
    assert.ok(Buffer.byteLength(JSON.stringify(result)) <= LIMITS.result);
    assert.equal(result.total, 8);
    assert.equal(result.offset, offset);
    assert.equal(typeof result.next_offset, 'number');
    names.push(...result.tools.map(tool => tool.name));
    assert.ok(result.next_offset > offset);
    offset = result.next_offset;
    if (!result.has_more) break;
  } while (offset < 8);
  assert.deepEqual(names, f.config.allowed_tools);
  await f.close();
});

for (const [mode, code] of [['cursor_loop', 'MCP_CURSOR_LOOP'], ['pages', 'MCP_DIRECTORY_LIMIT'],
  ['definition', 'MCP_DEFINITION_LIMIT'], ['directory_bytes', 'MCP_DIRECTORY_LIMIT'], ['count', 'MCP_DIRECTORY_LIMIT'], ['duplicate', 'MCP_DIRECTORY_INVALID']]) {
  test(`${mode} directory stops at a finite bound`, async t => {
    const f = await fixture(t, mode);
    const response = await f.exchange({});
    assert.equal(response.result.status, 'error');
    assert.equal(response.result.code, code);
    const events = await f.events();
    assert.ok(events.filter(e => e.event === 'list').length <= LIMITS.pages);
    assert.equal(events.filter(e => e.event === 'call').length, 0);
    await f.close();
  });
}

test('same connection rejects command/cwd/env/allowlist changes without leaking any values', async t => {
  const f = await fixture(t);
  await f.list();
  for (const change of [
    { server: { ...f.config.server, env: { ...f.config.server.env, TOKEN_SECRET: 'PRIVATE_ENV_VALUE' } } },
    { server: { ...f.config.server, command: [...f.config.server.command, 'PRIVATE_COMMAND_VALUE'] } },
    { server: { ...f.config.server, cwd: root } },
    { allowed_tools: ['write_note'] },
  ]) {
    const response = await f.exchange(change);
    assert.equal(response.result.code, 'MCP_CONFIGURATION_CHANGED');
    assert.ok(!JSON.stringify(response).includes('PRIVATE_'));
  }
  assert.equal((await f.events()).filter(e => e.event === 'spawn').length, 1);
  await f.close();
});

test('NDJSON requests process serially with opaque response IDs', async t => {
  const f = await fixture(t);
  const schema = (await f.list()).tools[0].schema_sha256;
  const first = f.send({ action: 'call', tool_name: 'write_note', schema_sha256: schema,
    arguments: { text: 'one', behavior: 'delay' }, invocation_id: 'serial:one' });
  const second = f.send({ action: 'call', tool_name: 'write_note', schema_sha256: schema,
    arguments: { text: 'two' }, invocation_id: 'serial:two' });
  assert.equal((await f.read()).id, first.id);
  assert.equal((await f.read()).id, second.id);
  assert.equal(await readFile(f.statePath, 'utf8'), 'one\ntwo\n');
  await f.close();
});

test('invalid internal packets and spawn exceptions return fixed codes without secrets', async t => {
  const f = await fixture(t);
  for (const overrides of [ { type: 'chat' }, { action: 'other' }, { id: 'id', unexpected: 'PRIVATE_INPUT' },
    { server: { ...f.config.server, command: ['relative-private'] } }, { timeout_ms: 0 },
    { allowed_tools: [] }, { action: 'call', arguments: [] }, { limit: 0 }, { limit: 65 } ]) {
    const response = await f.exchange(overrides);
    assert.equal(response.ok, false);
    assert.equal(response.error, 'MCP_INVALID_INPUT');
    assert.ok(!JSON.stringify(response).includes('PRIVATE_INPUT'));
  }
  assert.deepEqual(await f.events(), []);
  const missing = await f.exchange({ server: { ...f.config.server, command: [join(root, 'PRIVATE_MISSING_EXE.exe')] } });
  assert.equal(missing.result.status, 'error');
  assert.ok(!JSON.stringify(missing).includes('PRIVATE_MISSING'));
  await f.close();
});

test('timeout boundary matches the host 600-second contract', async t => {
  const f = await fixture(t);
  const accepted = await f.exchange({ timeout_ms: 600000 });
  assert.equal(accepted.result.status, 'ok');
  for (const value of [600001, 0, -1, 1.5]) {
    const rejected = await f.exchange({ timeout_ms: value });
    assert.equal(rejected.ok, false);
    assert.equal(rejected.error, 'MCP_INVALID_INPUT');
  }
  await f.close();
});

test('Windows Node startup requires explicit SYSTEMROOT in server.env', { skip: process.platform !== 'win32' }, async t => {
  const missing = await fixture(t);
  missing.config.server.env = { PYTHONIOENCODING: 'utf-8' };
  const response = await missing.exchange({});
  assert.equal(response.result.status, 'error');
  assert.equal(response.result.code, 'MCP_REQUEST_FAILED');
  assert.equal((await missing.events()).length, 0);
  await missing.close();
  const explicit = await fixture(t);
  explicit.config.server.env.PYTHONIOENCODING = 'utf-8';
  assert.equal((await explicit.exchange({})).result.status, 'ok');
  await explicit.close();
});

test('stdin EOF during an in-flight write closes the SDK child with no retry', async t => {
  const f = await fixture(t);
  const schema = (await f.list()).tools[0].schema_sha256;
  f.send({ action: 'call', tool_name: 'write_note', schema_sha256: schema,
    arguments: { text: 'eof-once', behavior: 'timeout' }, invocation_id: 'eof:once' });
  for (let i = 0; i < 100; i++) {
    if ((await f.events()).some(e => e.event === 'call')) break;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  await f.close();
  assert.equal(await readFile(f.statePath, 'utf8'), 'eof-once\n');
  assert.equal((await f.events()).filter(e => e.event === 'call').length, 1);
  assert.ok((await f.events()).some(e => e.event === 'exit'));
});

test('closed host stdout is handled without private exceptions on stderr', async t => {
  const f = await fixture(t);
  await f.list();
  f.proc.stdout.destroy();
  f.send({ action: 'list' });
  for (let i = 0; i < 100 && f.proc.exitCode === null; i++) await new Promise(resolve => setTimeout(resolve, 10));
  await f.close();
  assert.ok((await f.events()).some(e => e.event === 'exit'));
});

test('malformed and oversized NDJSON is bounded and does not start a server', async t => {
  const f = await fixture(t);
  f.proc.stdin.write('{"PRIVATE_PARSE":"bad"\n');
  assert.deepEqual(await f.read(), { id: null, ok: false, error: 'MCP_INVALID_INPUT' });
  f.proc.stdin.write('X'.repeat(LIMITS.packet + 1));
  assert.deepEqual(await f.read(), { id: null, ok: false, error: 'MCP_INPUT_LIMIT' });
  await f.close();
  assert.deepEqual(await f.events(), []);
});

test('SIGTERM closes SDK child on platforms that deliver catchable signals', { skip: process.platform === 'win32' }, async t => {
  const f = await fixture(t);
  await f.list();
  f.proc.kill('SIGTERM');
  await f.close();
  assert.ok((await f.events()).some(e => e.event === 'exit'));
});
