import { createHash } from 'node:crypto';
import { isAbsolute, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { TextDecoder } from 'node:util';
import { open, stat } from 'node:fs/promises';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { DEFAULT_INHERITED_ENV_VARS, StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { CallToolResultSchema, ListToolsResultSchema } from '@modelcontextprotocol/sdk/types.js';
import { AjvJsonSchemaValidator } from '@modelcontextprotocol/sdk/validation/ajv';
import Ajv from 'ajv';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { z } from 'zod';

export const LIMITS = Object.freeze({
  packet: 1024 * 1024,
  body: 64 * 1024,
  result: 256 * 1024,
  definition: 64 * 1024,
  directory: 8 * 1024 * 1024,
  tools: 4096,
  pages: 128,
  allowed: 64,
  depth: 64,
  timeout: 600000,
  // Larger than the internal packet: valid large terminals can be summarized.
  downstream: 4 * 1024 * 1024,
});

class SafeError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const bytes = value => Buffer.byteLength(JSON.stringify(value), 'utf8');
const digest = value => createHash('sha256').update(value, 'utf8').digest('hex');

// Construct the JSON string directly: object assignment would reorder integer keys
// and can mishandle the otherwise valid JSON key "__proto__".
export function canonicalJSON(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJSON).join(',')}]`;
  if (object(value)) {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJSON(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function schemaHash(tool) {
  return digest(canonicalJSON({ name: tool.name, inputSchema: tool.inputSchema }));
}

function boundedDepth(value) {
  const stack = [[value, 0]];
  while (stack.length) {
    const [item, depth] = stack.pop();
    if (depth > LIMITS.depth) throw new SafeError('MCP_DEPTH_LIMIT');
    if (item !== null && typeof item === 'object') {
      for (const child of Object.values(item)) stack.push([child, depth + 1]);
    }
  }
}

function validString(value, max = 256) {
  return typeof value === 'string' && value.length > 0 && !value.includes('\0') && Buffer.byteLength(value) <= max;
}

function onlyKeys(value, keys) {
  return Object.keys(value).every(key => keys.includes(key));
}

function validateRequest(request) {
  if (!object(request) || !validString(request.id) || request.type !== 'complete'
      || !['list', 'call'].includes(request.action)
      || !onlyKeys(request, ['type', 'id', 'action', 'server', 'allowed_tools', 'timeout_ms', 'offset', 'limit',
        'tool_name', 'schema_sha256', 'arguments', 'invocation_id'])) throw new SafeError('MCP_INVALID_INPUT');
  boundedDepth(request);
  const server = request.server;
  if (!object(server) || !onlyKeys(server, ['command', 'cwd', 'env'])
      || !Array.isArray(server.command) || !server.command.length || server.command.length > 128
      || !server.command.every(arg => typeof arg === 'string' && !arg.includes('\0'))
      || !isAbsolute(server.command[0]) || !validString(server.cwd, 32768) || !isAbsolute(server.cwd)
      || (server.env !== undefined && !object(server.env))) throw new SafeError('MCP_INVALID_INPUT');
  // On Windows cross-spawn may invoke cmd.exe for scripts; require a real binary.
  if (process.platform === 'win32' && !/\.exe$/i.test(server.command[0])) throw new SafeError('MCP_INVALID_INPUT');
  const env = server.env ?? {};
  const envKeys = new Set();
  for (const [key, value] of Object.entries(env)) {
    const normalized = process.platform === 'win32' ? key.toUpperCase() : key;
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || envKeys.has(normalized)
        || typeof value !== 'string' || value.includes('\0')) throw new SafeError('MCP_INVALID_INPUT');
    envKeys.add(normalized);
  }
  if (!Array.isArray(request.allowed_tools) || !request.allowed_tools.length
      || request.allowed_tools.length > LIMITS.allowed || !request.allowed_tools.every(name => validString(name))
      || new Set(request.allowed_tools).size !== request.allowed_tools.length) throw new SafeError('MCP_INVALID_INPUT');
  const timeout = request.timeout_ms ?? 30000;
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > LIMITS.timeout) throw new SafeError('MCP_INVALID_INPUT');
  if (request.action === 'list') {
    if (!Number.isSafeInteger(request.offset ?? 0) || (request.offset ?? 0) < 0
        || !Number.isSafeInteger(request.limit ?? 10) || (request.limit ?? 10) < 1
        || (request.limit ?? 10) > LIMITS.allowed) throw new SafeError('MCP_INVALID_INPUT');
  } else if (!validString(request.tool_name) || !/^[a-f0-9]{64}$/.test(request.schema_sha256 ?? '')
      || !object(request.arguments) || !validString(request.invocation_id, 512)) throw new SafeError('MCP_INVALID_INPUT');
  return { server: { command: server.command, cwd: server.cwd, env }, allowed_tools: [...request.allowed_tools].sort(), timeout };
}

// A fresh Ajv per schema prevents remote $id collisions and unbounded compilation
// caches. No defaults, coercion, property removal, asynchronous refs or network IO.
function schemaValidator(schema) {
  const draft = schema.$schema;
  let Constructor = Ajv2020;
  if (draft === 'http://json-schema.org/draft-07/schema#' || draft === 'https://json-schema.org/draft-07/schema#') Constructor = Ajv;
  else if (draft !== undefined && draft !== 'https://json-schema.org/draft/2020-12/schema') throw new SafeError('MCP_SCHEMA_UNSUPPORTED');
  const ajv = new Constructor({
    strict: true, allErrors: false, validateSchema: true, validateFormats: true,
    useDefaults: false, coerceTypes: false, removeAdditional: false,
  });
  addFormats(ajv);
  const validate = new AjvJsonSchemaValidator(ajv).getValidator(schema);
  if (schema.$async) throw new SafeError('MCP_SCHEMA_UNSUPPORTED');
  return validate;
}

function prefix(text, maxBytes) {
  const buffer = Buffer.from(text, 'utf8');
  if (buffer.length <= maxBytes) return text;
  const decoder = new TextDecoder('utf-8', { fatal: true });
  for (let end = maxBytes; end >= Math.max(0, maxBytes - 3); end--) {
    try { return decoder.decode(buffer.subarray(0, end)); } catch { /* incomplete UTF-8 sequence */ }
  }
  return '';
}

function resultSummary(value) {
  // Iterative serialization keeps valid deep structured results summarizable.
  // This hashes the complete parsed result's JSON value, not transport framing.
  const hash = createHash('sha256');
  let size = 0;
  const stack = [{ value }];
  while (stack.length) {
    const item = stack.pop();
    if ('text' in item) {
      hash.update(item.text, 'utf8');
      size += Buffer.byteLength(item.text);
    } else if (item.value !== null && typeof item.value === 'object') {
      const array = Array.isArray(item.value);
      const keys = Object.keys(item.value);
      stack.push({ text: array ? ']' : '}' });
      for (let index = keys.length - 1; index >= 0; index--) {
        const key = keys[index];
        stack.push({ value: item.value[key] });
        if (!array) stack.push({ text: `${JSON.stringify(key)}:` });
        if (index > 0) stack.push({ text: ',' });
      }
      stack.push({ text: array ? '[' : '{' });
    } else stack.push({ text: JSON.stringify(item.value) });
  }
  return { result_bytes: size, result_sha256: hash.digest('hex') };
}

function displayable(value) {
  try { boundedDepth(value); return true; } catch { return false; }
}

function terminal(raw, parsed) {
  const summary = resultSummary(raw);
  const mcp = { content: [] };
  if (parsed.isError !== undefined) mcp.isError = parsed.isError;
  const unsupported = [...new Set(parsed.content.filter(block => block.type !== 'text').map(block => block.type))].sort();
  let complete = unsupported.length === 0 && onlyKeys(raw, ['content', 'structuredContent', 'isError']);
  for (const block of raw.content ?? []) {
    if (block.type !== 'text') continue;
    if (mcp.content.length < 256 && displayable(block) && bytes({ ...mcp, content: [...mcp.content, block] }) <= LIMITS.body) {
      mcp.content.push(block);
      continue;
    }
    complete = false;
    if (mcp.content.length >= 256) break;
    // JSON escaping can expand text sixfold; binary search the actual JSON budget.
    let low = 0;
    let high = Math.min(Buffer.byteLength(block.text), LIMITS.body);
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      const candidate = { type: 'text', text: prefix(block.text, mid) };
      if (bytes({ ...mcp, content: [...mcp.content, candidate] }) <= LIMITS.body) low = mid;
      else high = mid - 1;
    }
    const text = prefix(block.text, low);
    if (text) mcp.content.push({ type: 'text', text });
    break;
  }
  if (raw.structuredContent !== undefined) {
    if (displayable(raw.structuredContent) && bytes({ ...mcp, structuredContent: raw.structuredContent }) <= LIMITS.body) mcp.structuredContent = raw.structuredContent;
    else complete = false;
  }
  // The SDK defaults missing content to []; preserve that protocol compatibility.
  const result = {
    status: parsed.isError ? 'error' : 'ok', complete: true,
    outcome: parsed.isError ? 'tool_error' : 'returned', mcp_result: mcp,
    representation_complete: complete, unsupported_content_types: unsupported,
    ...summary,
  };
  if (!complete) {
    result.code = 'MCP_REPRESENTATION_INCOMPLETE';
    result.result_excerpt = prefix(mcp.content.map(block => block.text).join('\n'), 1024);
  }
  return result;
}

function refused(code) {
  return { status: 'error', code, complete: true, outcome: 'not_started', message: '\u8c03\u7528\u672a\u5f00\u59cb\uff0c\u8bf7\u6838\u5bf9\u6388\u6743\u3001\u5b9a\u4e49\u548c\u53c2\u6570\u3002' };
}

const safeCode = error => error instanceof SafeError ? error.code : 'MCP_REQUEST_FAILED';

class Bridge {
  constructor() {
    this.configuration = undefined;
    this.client = undefined;
    this.transport = undefined;
    this.dead = false;
    this.closed = false;
    this.closing = undefined;
    this.active = undefined;
  }

  stop() {
    this.dead = true;
    this.active?.abort(new SafeError('MCP_CONNECTION_CLOSED'));
    if (!this.closing) {
      this.closing = (async () => {
        try { await this.client?.close(); } catch { /* never forward SDK diagnostics */ }
        try { await this.transport?.close(); } catch { /* SDK handles child termination */ }
      })();
    }
    return this.closing;
  }

  async close() {
    this.closed = true;
    await this.stop();
  }

  options(deadline) {
    const remaining = deadline - performance.now();
    if (remaining <= 0) throw new SafeError('MCP_TIMEOUT');
    if (this.dead || this.closed || this.active.signal.aborted) throw new SafeError('MCP_CONNECTION_CLOSED');
    return { signal: this.active.signal, timeout: remaining, maxTotalTimeout: remaining, resetTimeoutOnProgress: false };
  }

  async connect(configuration, deadline) {
    if (this.dead || this.closed) throw new SafeError('MCP_CONNECTION_CLOSED');
    if (this.client) return;
    const { command, cwd, env } = configuration.server;
    const executable = await stat(command[0]);
    if (!executable.isFile() || !(await stat(cwd)).isDirectory()) throw new SafeError('MCP_INVALID_SERVER');
    if (process.platform === 'win32') {
      const file = await open(command[0], 'r');
      try {
        const header = Buffer.alloc(2);
        await file.read(header, 0, 2, 0);
        if (header.toString('ascii') !== 'MZ') throw new SafeError('MCP_INVALID_SERVER');
      } finally { await file.close(); }
    }
    // SDK 1.32.1 merges defaults even for env={}. Undefined explicitly removes
    // those keys at spawn; supplied values are the only downstream environment.
    const childEnv = Object.fromEntries(DEFAULT_INHERITED_ENV_VARS.map(key => [key, undefined]));
    for (const [key, value] of Object.entries(env)) childEnv[process.platform === 'win32' ? key.toUpperCase() : key] = value;
    this.transport = new StdioClientTransport({
      command: command[0], args: command.slice(1), cwd, env: childEnv,
      stderr: 'pipe', maxBufferSize: LIMITS.downstream,
    });
    this.transport.stderr.on('error', () => {});
    this.transport.stderr.resume();
    this.client = new Client({ name: 'cater-mcp-bridge', version: '0.1.0' }, {
      capabilities: {}, jsonSchemaValidator: { getValidator: schemaValidator },
    });
    this.client.onerror = () => { void this.stop(); };
    this.client.onclose = () => { this.dead = true; this.active?.abort(new SafeError('MCP_CONNECTION_CLOSED')); };
    await this.client.connect(this.transport, this.options(deadline));
  }

  async directory(allowed, deadline) {
    const tools = new Map();
    const names = new Set();
    const cursors = new Set();
    let cursor;
    let totalBytes = 0;
    let count = 0;
    for (let page = 0; page < LIMITS.pages; page++) {
      let raw;
      const schema = z.preprocess(value => { raw = value; return value; }, ListToolsResultSchema);
      // SDK request owns framing, IDs, protocol validation and cancellation.
      // Unlike listTools(), it does not compile unbounded/non-allowlisted schemas
      // or replace the output validators on every cursor page.
      const reply = await this.client.request({ method: 'tools/list', params: cursor === undefined ? {} : { cursor } }, schema, this.options(deadline));
      boundedDepth(raw);
      totalBytes += bytes(raw);
      count += reply.tools.length;
      if (totalBytes > LIMITS.directory || count > LIMITS.tools) throw new SafeError('MCP_DIRECTORY_LIMIT');
      for (let index = 0; index < reply.tools.length; index++) {
        const tool = reply.tools[index];
        if (bytes(raw.tools[index]) > LIMITS.definition) throw new SafeError('MCP_DEFINITION_LIMIT');
        if (!validString(tool.name) || names.has(tool.name)) throw new SafeError('MCP_DIRECTORY_INVALID');
        names.add(tool.name);
        if (allowed.has(tool.name)) tools.set(tool.name, tool);
      }
      if (reply.nextCursor === undefined) return tools;
      if (!validString(reply.nextCursor, 4096) || cursors.has(reply.nextCursor)) throw new SafeError('MCP_CURSOR_LOOP');
      cursors.add(reply.nextCursor);
      cursor = reply.nextCursor;
    }
    throw new SafeError('MCP_DIRECTORY_LIMIT');
  }

  async handle(request) {
    const id = object(request) && validString(request.id) ? request.id : null;
    let configuration;
    try {
      if (bytes(request) + 1 > LIMITS.packet) throw new SafeError('MCP_INPUT_LIMIT');
      configuration = validateRequest(request);
    } catch (error) {
      return { id, ok: false, error: safeCode(error) };
    }
    let dispatched = false;
    this.active = new AbortController();
    const deadline = performance.now() + configuration.timeout;
    try {
      const fingerprint = canonicalJSON({ server: configuration.server, allowed_tools: configuration.allowed_tools });
      if (this.configuration !== undefined && fingerprint !== this.configuration) throw new SafeError('MCP_CONFIGURATION_CHANGED');
      this.configuration = fingerprint;
      const allowed = new Set(configuration.allowed_tools);
      if (request.action === 'call' && !allowed.has(request.tool_name)) throw new SafeError('MCP_TOOL_NOT_ALLOWED');
      await this.connect(configuration, deadline);
      const tools = await this.directory(allowed, deadline);
      if (request.action === 'list') {
        const all = [...tools.values()];
        const offset = Math.min(request.offset ?? 0, all.length);
        const entries = [];
        for (const tool of all.slice(offset, offset + (request.limit ?? 10))) {
          const entry = {
            name: tool.name, title: tool.title ?? '', description: tool.description ?? '',
            input_schema: tool.inputSchema, schema_sha256: schemaHash(tool),
          };
          if (entries.length >= 20 || bytes(entries) + bytes(entry) > LIMITS.result - 8192) break;
          entries.push(entry);
        }
        const hasMore = offset + entries.length < all.length;
        return { id, ok: true, result: {
          status: 'ok', operation: 'list', tools: entries, total: all.length, offset,
          next_offset: offset + entries.length, has_more: hasMore, complete: true,
        } };
      }
      const tool = tools.get(request.tool_name);
      if (!tool) throw new SafeError('MCP_TOOL_NOT_FOUND');
      if (schemaHash(tool) !== request.schema_sha256) throw new SafeError('MCP_SCHEMA_CHANGED');
      if (tool.execution?.taskSupport === 'required') throw new SafeError('MCP_TASKS_UNSUPPORTED');
      let validate;
      try { validate = schemaValidator(tool.inputSchema); } catch { throw new SafeError('MCP_SCHEMA_UNSUPPORTED'); }
      if (!validate(request.arguments).valid) throw new SafeError('MCP_ARGUMENTS_INVALID');
      let outputValidator;
      try { if (tool.outputSchema) outputValidator = schemaValidator(tool.outputSchema); }
      catch { throw new SafeError('MCP_SCHEMA_UNSUPPORTED'); }
      const options = this.options(deadline);
      let raw;
      const resultSchema = z.preprocess(value => { raw = value; return value; }, CallToolResultSchema);
      const params = {
        name: tool.name, arguments: request.arguments,
        _meta: { 'cater/invocation_id': request.invocation_id },
      };
      // No path below this boundary may claim not_started, including SDK output
      // schema rejection and representation/serialization failures.
      dispatched = true;
      const result = await this.client.callTool(params, resultSchema, options);
      // Avoid the SDK's private page-local metadata cache. Its public validator
      // provider applies the same output validation to the selected definition.
      if (outputValidator && ((!result.isError && result.structuredContent === undefined)
          || (result.structuredContent !== undefined && !outputValidator(result.structuredContent).valid))) {
        throw new SafeError('MCP_OUTPUT_INVALID');
      }
      return { id, ok: true, result: terminal(raw, result) };
    } catch (error) {
      if (dispatched) {
        void this.stop();
        return { id, ok: true, result: {
          status: 'error', code: 'MCP_EFFECTS_UNKNOWN', complete: false, outcome: 'effects_unknown',
          message: '\u8c03\u7528\u7ed3\u679c\u65e0\u6cd5\u786e\u8ba4\uff0c\u8bf7\u6838\u5bf9\u5b9e\u9645\u72b6\u6001\uff0c\u4e0d\u8981\u81ea\u52a8\u91cd\u8bd5\u3002',
        } };
      }
      const code = safeCode(error);
      if (!(error instanceof SafeError)) void this.stop();
      if (request.action === 'call') return { id, ok: true, result: refused(code) };
      return { id, ok: true, result: { status: 'error', operation: 'list', code, complete: true,
        message: '\u76ee\u5f55\u8bfb\u53d6\u5931\u8d25\uff0c\u8bf7\u6838\u5bf9\u670d\u52a1\u914d\u7f6e\u3002' } };
    } finally {
      this.active = undefined;
    }
  }
}

async function writePacket(packet) {
  let line;
  try {
    line = JSON.stringify(packet);
    if (Buffer.byteLength(line) + 1 > LIMITS.packet) throw new SafeError('MCP_OUTPUT_LIMIT');
  } catch {
    line = JSON.stringify({ id: packet.id, ok: false, error: 'MCP_OUTPUT_LIMIT' });
  }
  await new Promise((accept, reject) => process.stdout.write(`${line}\n`, error => error ? reject(error) : accept()));
}

export async function main() {
  // This is a dedicated IPC process. Windows/libuv can reinsert absent essential
  // variables from its own environment even when spawn.env is explicit. Scrub
  // here as well as overriding SDK defaults; never mutate the Python host env.
  for (const key of Object.keys(process.env)) delete process.env[key];
  const bridge = new Bridge();
  let shuttingDown = false;
  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    void bridge.close();
    process.stdin.destroy();
  };
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);
  process.stdin.once('end', shutdown);
  process.stdout.once('error', shutdown);
  let pending = Buffer.alloc(0);
  try {
    for await (const chunk of process.stdin) {
      let start = 0;
      for (let index = 0; index < chunk.length; index++) {
        if (chunk[index] !== 10) continue;
        const part = chunk.subarray(start, index);
        if (pending.length + part.length + 1 > LIMITS.packet) throw new SafeError('MCP_INPUT_LIMIT');
        const line = Buffer.concat([pending, part]);
        pending = Buffer.alloc(0);
        let packet;
        try { packet = await bridge.handle(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(line))); }
        catch { packet = { id: null, ok: false, error: 'MCP_INVALID_INPUT' }; }
        await writePacket(packet);
        start = index + 1;
        if (shuttingDown) break;
      }
      if (shuttingDown) break;
      pending = Buffer.concat([pending, chunk.subarray(start)]);
      if (pending.length >= LIMITS.packet) throw new SafeError('MCP_INPUT_LIMIT');
    }
    if (pending.length) await writePacket({ id: null, ok: false, error: 'MCP_INVALID_INPUT' });
  } catch (error) {
    if (!shuttingDown) {
      try { await writePacket({ id: null, ok: false, error: safeCode(error) }); } catch { /* closed host output */ }
    }
  } finally {
    await bridge.close();
    process.stdin.destroy();
    process.removeListener('SIGINT', shutdown);
    process.removeListener('SIGTERM', shutdown);
    process.stdin.removeListener('end', shutdown);
    process.stdout.removeListener('error', shutdown);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => { process.exitCode = 1; });
}
