/**
 * Coding Intelligence's bounded DeepSeek Harness plugin.
 *
 * This replaces the shipped final-text-only headless runner. Configuration
 * comes only from a validated parent; the model gets list/read/search/edit/test.
 */

import { createHash, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { closeSync, fsyncSync, openSync, writeSync } from "node:fs";
import {
  lstat,
  mkdir,
  readFile,
  realpath,
  readdir,
  stat,
  writeFile,
} from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";
import { defineTool } from "@deepseek-ai/dsh-tools";

export const name = "ci-deepseek-production-runner";
export const inject = ["agentDefaultModel", "agents", "sessions", "systemPrompt", "tools"];

const ALLOWED_TOOLS = Object.freeze(["list", "read", "search", "edit", "test"]);
const MAX_FILE_BYTES = 1_048_576;
const MAX_EDIT_BYTES = 524_288;
const MAX_MATCHES = 500;
const MAX_TOOL_OUTPUT = 131_072;
const WINDOWS_RESERVED_NAMES = new Set([
  "CON", "PRN", "AUX", "NUL",
  "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
  "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
]);
let eventLedger = null;

function requiredEnv(key) {
  const value = process.env[key];
  if (typeof value !== "string" || value.length === 0 || value.includes("\0")) {
    throw new Error(`${key} is required`);
  }
  return value;
}

function positiveInteger(key, fallback, maximum = Number.MAX_SAFE_INTEGER) {
  const raw = process.env[key] ?? String(fallback);
  if (!/^[1-9][0-9]*$/u.test(raw)) throw new Error(`${key} must be a positive integer`);
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value > maximum) {
    throw new Error(`${key} is outside its supported range`);
  }
  return value;
}

/** Parse JSON while rejecting duplicate keys at every object depth. */
export function parseJsonStrict(text, label = "JSON") {
  if (typeof text !== "string") throw new Error(`${label} must be text`);
  let offset = 0;
  const fail = (message) => {
    throw new Error(`${label}: ${message} at offset ${offset}`);
  };
  const whitespace = () => {
    while (offset < text.length && /[\u0009\u000a\u000d\u0020]/u.test(text[offset])) offset += 1;
  };
  const string = () => {
    if (text[offset] !== '"') fail("expected string");
    const start = offset;
    offset += 1;
    while (offset < text.length) {
      const character = text[offset++];
      if (character === '"') {
        const raw = text.slice(start, offset);
        try {
          return JSON.parse(raw);
        } catch {
          fail("invalid string");
        }
      }
      if (character === "\\") {
        if (offset >= text.length) fail("unterminated escape");
        const escaped = text[offset++];
        if (escaped === "u") {
          if (!/^[0-9a-fA-F]{4}$/u.test(text.slice(offset, offset + 4))) fail("invalid unicode escape");
          offset += 4;
        } else if (!'"\\/bfnrt'.includes(escaped)) {
          fail("invalid escape");
        }
      } else if (character.charCodeAt(0) < 0x20) {
        fail("unescaped control character");
      }
    }
    fail("unterminated string");
  };
  const number = () => {
    const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/u.exec(text.slice(offset));
    if (match === null) fail("invalid number");
    offset += match[0].length;
    const parsed = Number(match[0]);
    if (!Number.isFinite(parsed)) fail("non-finite number");
    return parsed;
  };
  const value = () => {
    whitespace();
    const character = text[offset];
    if (character === '"') return string();
    if (character === "{") {
      offset += 1;
      whitespace();
      const output = Object.create(null);
      const keys = new Set();
      if (text[offset] === "}") {
        offset += 1;
        return output;
      }
      while (true) {
        whitespace();
        const key = string();
        if (keys.has(key)) fail(`duplicate object key ${JSON.stringify(key)}`);
        keys.add(key);
        whitespace();
        if (text[offset++] !== ":") fail("expected colon");
        output[key] = value();
        whitespace();
        const delimiter = text[offset++];
        if (delimiter === "}") return output;
        if (delimiter !== ",") fail("expected comma or object close");
      }
    }
    if (character === "[") {
      offset += 1;
      whitespace();
      const output = [];
      if (text[offset] === "]") {
        offset += 1;
        return output;
      }
      while (true) {
        output.push(value());
        whitespace();
        const delimiter = text[offset++];
        if (delimiter === "]") return output;
        if (delimiter !== ",") fail("expected comma or array close");
      }
    }
    for (const [literal, parsed] of [["true", true], ["false", false], ["null", null]]) {
      if (text.startsWith(literal, offset)) {
        offset += literal.length;
        return parsed;
      }
    }
    return number();
  };
  const parsed = value();
  whitespace();
  if (offset !== text.length) fail("trailing data");
  return parsed;
}

function stringArray(key) {
  const parsed = parseJsonStrict(requiredEnv(key), key);
  if (!Array.isArray(parsed) || parsed.length === 0 || !parsed.every((item) => typeof item === "string" && item.length > 0)) {
    throw new Error(`${key} must be a non-empty string array`);
  }
  return [...new Set(parsed.map(normalizeRelative))];
}

function commandArray(key) {
  const parsed = parseJsonStrict(requiredEnv(key), key);
  if (!Array.isArray(parsed) || parsed.length === 0 || !parsed.every((item) => typeof item === "string" && item.length > 0 && !item.includes("\0"))) {
    throw new Error(`${key} must be a non-empty argv array`);
  }
  return parsed;
}

function normalizeRelative(raw) {
  if (typeof raw !== "string" || raw.length === 0 || raw.includes("\0") || isAbsolute(raw)) {
    throw new Error(`unsafe relative path: ${JSON.stringify(raw)}`);
  }
  const text = raw.replaceAll("\\", "/");
  const parts = text.split("/");
  if (parts.some((part) => {
    const deviceStem = part.split(".", 1)[0].toUpperCase();
    return part.length === 0
      || part === "."
      || part === ".."
      || part.includes(":")
      || part.endsWith(".")
      || part.endsWith(" ")
      || WINDOWS_RESERVED_NAMES.has(deviceStem);
  })) {
    throw new Error(`unsafe relative path: ${JSON.stringify(raw)}`);
  }
  return parts.join("/");
}

function allowed(path, roots) {
  const normalized = normalizeRelative(path);
  return roots.some((root) => normalized === root || normalized.startsWith(`${root}/`));
}

function safelyAllowed(path, roots) {
  try {
    return allowed(path, roots);
  } catch {
    return false;
  }
}

async function scopedPath(stage, raw, roots, requireExisting = true) {
  if (!allowed(raw, roots)) throw new Error(`path is outside declared scope: ${JSON.stringify(raw)}`);
  const normalized = normalizeRelative(raw);
  const target = resolve(stage, ...normalized.split("/"));
  const rel = relative(stage, target);
  if (!rel || rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error(`path escapes stage: ${JSON.stringify(raw)}`);
  }
  let current = stage;
  for (const segment of normalized.split("/")) {
    current = resolve(current, segment);
    try {
      const info = await lstat(current);
      if (info.isSymbolicLink()) throw new Error(`symbolic links are forbidden: ${normalized}`);
    } catch (error) {
      if (error?.code === "ENOENT") break;
      throw error;
    }
  }
  if (requireExisting) {
    const [canonicalStage, canonicalTarget] = await Promise.all([realpath(stage), realpath(target)]);
    const realRel = relative(canonicalStage, canonicalTarget);
    if (realRel === ".." || realRel.startsWith(`..${sep}`) || isAbsolute(realRel)) {
      throw new Error(`real path escapes stage: ${JSON.stringify(raw)}`);
    }
  }
  return target;
}

async function collectFiles(stage, target, roots, output) {
  const info = await stat(target);
  if (info.isFile()) {
    output.add(target);
    return;
  }
  if (!info.isDirectory()) return;
  for (const item of await readdir(target, { withFileTypes: true })) {
    const child = resolve(target, item.name);
    const rel = relative(stage, child).split(sep).join("/");
    if (!allowed(rel, roots)) continue;
    if (item.isSymbolicLink()) throw new Error(`symbolic links are forbidden: ${rel}`);
    if (item.isDirectory()) await collectFiles(stage, child, roots, output);
    else if (item.isFile()) output.add(child);
  }
}

function emit(value) {
  const line = `${JSON.stringify(value)}\n`;
  if (eventLedger === null) throw new Error("event ledger is not initialized");
  writeSync(eventLedger, line, null, "utf8");
  fsyncSync(eventLedger);
  process.stdout.write(line);
}

function closeEventLedger() {
  if (eventLedger === null) return;
  closeSync(eventLedger);
  eventLedger = null;
}

function digest(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function textOutput(schema) {
  return {
    schema,
    render: (_args, value) => [{ type: "text", text: JSON.stringify(value).slice(0, MAX_TOOL_OUTPUT) }],
  };
}

function runCommand(command, cwd, timeoutMs, signal) {
  return new Promise((resolvePromise, rejectPromise) => {
    const childEnv = { ...process.env };
    for (const key of Object.keys(childEnv)) {
      if (key.startsWith("CI_ADAPTER_") || key.startsWith("DSH_")) delete childEnv[key];
    }
    childEnv.PYTHONDONTWRITEBYTECODE = "1";
    childEnv.CI_MODEL_TOOL_TEST = "1";
    const child = spawn(command[0], command.slice(1), {
      cwd,
      env: childEnv,
      shell: false,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const terminate = () => {
      if (child.exitCode === null) child.kill("SIGTERM");
    };
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      if (error) rejectPromise(error);
      else resolvePromise(result);
    };
    const abort = () => {
      terminate();
      finish(new Error("test execution aborted"));
    };
    const timer = setTimeout(() => {
      terminate();
      finish(new Error(`test timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout = (stdout + String(chunk)).slice(-16_384); });
    child.stderr.on("data", (chunk) => { stderr = (stderr + String(chunk)).slice(-16_384); });
    child.once("error", (error) => finish(error));
    child.once("close", (code, childSignal) => finish(null, { code, signal: childSignal, stdout, stderr }));
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

function prompt(objective, readable, mutable) {
  return [
    "Complete this coding objective in the isolated live-production stage.",
    `Objective: ${objective}`,
    `Readable paths: ${JSON.stringify(readable)}`,
    `Mutable paths: ${JSON.stringify(mutable)}`,
    "Start with list path '.' to see the repository root, then use list, read, and search to understand it before editing.",
    "Make every coordinated scoped edit needed to finish the objective.",
    "You may run test once before editing to establish the real baseline. After diagnosis, act; do not keep rereading files whose relevant behavior is already known.",
    "After meaningful changes, run test again. If it fails, repair only from the new failure evidence; do not repeat an identical failed test without an intervening edit.",
    "Finish only after the latest edit has a passing test, then report the completed result concisely.",
  ].join("\n");
}

function installTools(ctx, state) {
  const string = { type: "string" };
  ctx.systemPrompt.section({
    name: "ci:production-contract",
    order: 1,
    text: "You are the primary local coding agent. Inspect the repository, make all coordinated scoped edits required by the objective, and use the real build or compiler result to finish. List/read/search/edit are stage-scoped; test runs the repository's selected real command. Never claim an unobserved result or repeat an identical failed action.",
  });

  ctx.tools.register(defineTool({
    name: "list",
    description: "List files and directories at one declared model-readable staged path.",
    parameters: { path: { ...string, required: true } },
    output: textOutput({
      type: "object", additionalProperties: false, properties: {
        path: { type: "string", required: true },
        entries: { type: "array", required: true, items: { type: "string" } },
        truncated: { type: "boolean", required: true },
      },
    }),
    isConcurrencySafe: () => true,
    async execute(args) {
      const target = args.path === "."
        ? state.stage
        : await scopedPath(state.stage, args.path, state.readable);
      const info = await stat(target);
      if (!info.isDirectory()) throw new Error(`list target is not a directory: ${args.path}`);
      const entries = [];
      for (const item of await readdir(target, { withFileTypes: true })) {
        entries.push(`${item.isDirectory() ? "dir" : "file"}\t${item.name}`);
        if (entries.length >= 500) break;
      }
      entries.sort((left, right) => left.localeCompare(right));
      return {
        path: args.path === "." ? "." : normalizeRelative(args.path),
        entries,
        truncated: entries.length >= 500,
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: "read",
    description: "Read one declared model-readable UTF-8 file from the disposable stage.",
    parameters: { path: { ...string, required: true } },
    output: textOutput({
      type: "object", additionalProperties: false, properties: {
        path: { type: "string", required: true },
        text: { type: "string", required: true },
        bytes: { type: "integer", required: true },
      },
    }),
    isConcurrencySafe: () => true,
    async execute(args) {
      const target = await scopedPath(state.stage, args.path, state.readable);
      const info = await stat(target);
      if (!info.isFile()) throw new Error(`read target is not a file: ${args.path}`);
      if (info.size > MAX_FILE_BYTES) throw new Error(`read target exceeds ${MAX_FILE_BYTES} bytes`);
      return { path: normalizeRelative(args.path), text: await readFile(target, "utf8"), bytes: info.size };
    },
  }));

  ctx.tools.register(defineTool({
    name: "search",
    description: "Literal case-sensitive search over declared model-readable staged files.",
    parameters: { query: { ...string, required: true }, path: string },
    output: textOutput({
      type: "object", additionalProperties: false, properties: {
        query: { type: "string", required: true },
        matches: { type: "array", required: true, items: { type: "string" } },
        truncated: { type: "boolean", required: true },
      },
    }),
    isConcurrencySafe: () => true,
    async execute(args) {
      if (typeof args.query !== "string" || args.query.length < 1 || args.query.length > 512) {
        throw new Error("search query must contain 1..512 characters");
      }
      const roots = args.path === undefined ? state.readable : [normalizeRelative(args.path)];
      if (args.path !== undefined && !allowed(args.path, state.readable)) {
        throw new Error("search path is outside readable scope");
      }
      const files = new Set();
      for (const root of roots) {
        await collectFiles(state.stage, await scopedPath(state.stage, root, state.readable), state.readable, files);
      }
      const matches = [];
      for (const file of [...files].sort()) {
        const info = await stat(file);
        if (info.size > MAX_FILE_BYTES) continue;
        let content;
        try { content = await readFile(file, "utf8"); } catch { continue; }
        const fileName = relative(state.stage, file).split(sep).join("/");
        for (const [index, line] of content.split(/\r?\n/u).entries()) {
          if (line.includes(args.query)) matches.push(`${fileName}:${index + 1}:${line.slice(0, 500)}`);
          if (matches.length >= MAX_MATCHES) break;
        }
        if (matches.length >= MAX_MATCHES) break;
      }
      return { query: args.query, matches, truncated: matches.length >= MAX_MATCHES };
    },
  }));

  ctx.tools.register(defineTool({
    name: "edit",
    description: "Perform one exact-text replacement in one declared mutable staged file. Call again for additional coordinated edits.",
    parameters: {
      path: { ...string, required: true },
      old_text: { ...string, required: true },
      new_text: { ...string, required: true },
    },
    output: textOutput({
      type: "object", additionalProperties: false, properties: {
        path: { type: "string", required: true },
        created: { type: "boolean", required: true },
        before_sha256: { type: "string", required: true },
        after_sha256: { type: "string", required: true },
      },
    }),
    async execute(args) {
      if (args.new_text.length > MAX_EDIT_BYTES) throw new Error("replacement exceeds 512 KiB");
      const normalized = normalizeRelative(args.path);
      const target = await scopedPath(state.stage, normalized, state.mutable, false);
      let original;
      let created = false;
      try {
        original = await readFile(target, "utf8");
      } catch (error) {
        if (error?.code !== "ENOENT" || args.old_text !== "") throw error;
        original = "";
        created = true;
      }
      const occurrences = args.old_text === "" ? 0 : original.split(args.old_text).length - 1;
      if (!created && occurrences !== 1) {
        throw new Error(
          `old_text must occur exactly once; found ${occurrences}; `
          + `received=${JSON.stringify(args.old_text)} file_bytes=${Buffer.byteLength(original, "utf8")}`,
        );
      }
      const updated = created ? args.new_text : original.replace(args.old_text, args.new_text);
      if (created) {
        await mkdir(resolve(target, ".."), { recursive: true });
        await writeFile(target, updated, { encoding: "utf8", flag: "wx" });
      } else {
        await writeFile(target, updated, "utf8");
      }
      state.editSucceeded = true;
      state.testSucceeded = false;
      state.editGeneration += 1;
      return {
        path: normalized,
        created,
        before_sha256: createHash("sha256").update(original).digest("hex"),
        after_sha256: createHash("sha256").update(updated).digest("hex"),
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: "test",
    description: "Run the selected real repository command for a baseline or after edits. A failed result should drive a targeted repair before retesting.",
    parameters: {},
    output: textOutput({
      type: "object", additionalProperties: false, properties: {
        exit_code: { type: "integer", required: true },
        passed: { type: "boolean", required: true },
        stdout_tail: { type: "string", required: true },
        stderr_tail: { type: "string", required: true },
      },
    }),
    async execute(_args, exec) {
      const result = await runCommand(state.testCommand, state.stage, state.testTimeoutMs, exec.signal);
      state.testSucceeded = result.code === 0;
      const outcome = {
        exit_code: result.code ?? -1,
        passed: result.code === 0,
        stdout_tail: result.stdout,
        stderr_tail: result.stderr,
      };
      state.toolOutcomes.set(String(exec.callId), outcome);
      return outcome;
    },
  }));
}

function installGuards(ctx, state) {
  const callFacts = new Map();
  const rawArgumentChunks = new Map();
  ctx.on("session/event", (_session, event) => {
    if (event.type === "assistant/chunk") {
      const chunk = event.data.chunk;
      if (chunk?.type === "tool-call-delta" && typeof chunk.argumentsDelta === "string") {
        const callId = String(chunk.id);
        rawArgumentChunks.set(callId, (rawArgumentChunks.get(callId) ?? "") + chunk.argumentsDelta);
      }
    }
    if (event.type === "tool/call") {
      const data = event.data;
      const callId = String(data.callId);
      const rawArguments = rawArgumentChunks.get(callId) ?? data.arguments;
      let strictArgs;
      let parseError;
      try {
        strictArgs = parseJsonStrict(rawArguments, `tool arguments ${data.callId}`);
      } catch (error) {
        parseError = error instanceof Error ? error.message : String(error);
      }
      callFacts.set(callId, { raw: rawArguments, strictArgs, parseError });
    }
    if (event.type === "step/start") {
      const key = `${event.data.turn}:${event.data.step}`;
      const attempts = (state.requestAttempts.get(key) ?? 0) + 1;
      state.requestAttempts.set(key, attempts);
      state.modelCalls += 1;
      emit({ type: "turn_start", turn: event.data.turn, step: event.data.step, attempt: attempts });
      if (state.modelCalls > state.maxTurns || attempts > 1) {
        emit({
          type: attempts > 1 ? "automatic_retry_start" : "model_budget_exceeded",
          turn: event.data.turn,
          step: event.data.step,
          attempt: attempts,
        });
      }
    }
    if (event.type === "assistant/message" && event.data.usage) {
      const usage = event.data.usage;
      const input = Number(usage.inputTokens ?? 0)
        + Number(usage.cacheReadTokens ?? 0)
        + Number(usage.cacheWriteTokens ?? 0);
      const output = Number(usage.outputTokens ?? 0);
      state.usage.input += input;
      state.usage.output += output;
      state.usage.total += input + output;
    }
  });

  ctx.on("agent/request-error", async (payload, next) => {
    emit({
      type: "model_request_error",
      turn: payload.turn,
      step: payload.step,
      provider: payload.provider,
      error: {
        code: payload.failure?.code ?? "unknown",
        message: payload.failure?.message ?? "model request failed",
      },
    });
    return await next();
  });

  ctx.on("tools/pre-execute", async (exec, next) => {
    state.toolCalls += 1;
    const callId = String(exec.callId);
    const fact = callFacts.get(callId);
    let denial;
    if (!ALLOWED_TOOLS.includes(exec.name)) denial = "tool is outside the four-tool allowlist";
    else if (state.toolCalls > state.maxToolCalls) denial = "tool-call budget exceeded";
    else if (state.callIds.has(callId)) denial = "duplicate tool call id";
    else if (fact?.parseError) denial = fact.parseError;
    else if (fact === undefined) denial = "tool call lacks its raw durable event";
    state.callIds.add(callId);

    if (exec.name === "edit") {
      state.editCalls += 1;
      if (state.editCalls > state.maxEditCalls) denial ??= "production edit-call budget exceeded";
      if (typeof exec.arguments?.path !== "string" || !safelyAllowed(exec.arguments.path, state.mutable)) {
        denial ??= "edit path is outside mutable scope";
      }
    } else if (exec.name === "list" || exec.name === "read") {
      const rootList = exec.name === "list" && exec.arguments?.path === ".";
      if (typeof exec.arguments?.path !== "string" || (!rootList && !safelyAllowed(exec.arguments.path, state.readable))) {
        denial ??= `${exec.name} path is outside readable scope`;
      }
    } else if (exec.name === "search") {
      if (exec.arguments?.path !== undefined && (typeof exec.arguments.path !== "string" || !safelyAllowed(exec.arguments.path, state.readable))) {
        denial ??= "search path is outside readable scope";
      }
    } else if (exec.name === "test") {
      state.testCalls += 1;
      if (state.testCalls > state.maxTestCalls) denial ??= "production test-call budget exceeded";
    }

    const sideEffect = exec.name === "edit" || exec.name === "test";
    const effectId = sideEffect
      ? `effect-${digest({ run: state.runId, name: exec.name, arguments: exec.arguments, editGeneration: state.editGeneration }).slice(0, 32)}`
      : null;
    if (effectId !== null && state.effectIds.has(effectId)) denial = "duplicate side-effect identity";
    if (effectId !== null) state.effectIds.add(effectId);

    emit({
      type: "tool_execution_start",
      toolCallId: callId,
      effectId,
      toolName: exec.name,
      args: exec.arguments,
      intent: { recorded: true, side_effect: sideEffect },
      denied: denial ?? null,
    });
    if (denial !== undefined) return { kind: "deny", reason: denial };
    return await next();
  });

  ctx.on("tools/result", (exec, result) => {
    const outcome = state.toolOutcomes.get(String(exec.callId));
    const testFailed = exec.name === "test" && outcome?.passed === false;
    emit({
      type: "tool_execution_end",
      toolCallId: String(exec.callId),
      toolName: exec.name,
      isError: Boolean(result.isError) || testFailed,
      result: {
        content: result.content,
        details: {
          path: typeof exec.arguments?.path === "string" ? exec.arguments.path : null,
          exitCode: outcome?.exit_code ?? null,
          passed: outcome?.passed ?? null,
        },
      },
    });
  });
}

async function run(ctx, state) {
  const exit = ctx.get("appExit");
  if (exit === undefined) throw new Error("appExit service is unavailable");
  let handle;
  try {
    await ctx.get("loader")?.await();
    const visible = ctx.tools.schemas().map((schema) => schema.name).sort();
    if (JSON.stringify(visible) !== JSON.stringify([...ALLOWED_TOOLS].sort())) {
      throw new Error(`unexpected model tool surface: ${JSON.stringify(visible)}`);
    }
    const selection = ctx.agentDefaultModel.currentSelection();
    handle = await ctx.agents.create({
      sessionId: SessionId(`ci-${randomUUID()}`),
      meta: { cwd: state.stage },
      agentOptions: {
        provider: selection.provider,
        model: selection.model,
        maxTokens: state.maxOutputTokens,
      },
      setup: (agentCtx) => {
        installModelSelection(agentCtx, { current: selection, assembled: undefined });
      },
    });
    const { agent } = handle;
    await agent.whenIdle();
    agent.followup(createUserMessage({
      content: [{ type: "text", text: prompt(state.objective, state.readable, state.mutable) }],
      source: { kind: "user" },
    }));
    await agent.whenIdle();
    await ctx.sessions.flush(agent.session);
    const terminal = [...agent.session.events].reverse().find((event) => event.type === "turn/end");
    const reason = terminal?.data?.reason;
    const completed = reason?.kind === "completed"
      && state.editSucceeded
      && state.testSucceeded
      && state.modelCalls <= state.maxTurns
      && state.toolCalls <= state.maxToolCalls;
    emit({
      type: "agent_end",
      runId: state.runId,
      status: completed ? "completed" : "failed",
      reason: completed
        ? reason
        : {
            kind: "error",
            error: {
              code: reason?.kind === "completed" ? "BOUNDED_TASK_INCOMPLETE" : "AGENT_TURN_FAILED",
              message: reason?.kind === "completed"
                ? "agent ended without a successful edit followed by a passing test"
                : "agent turn did not complete",
            },
          },
      modelCalls: state.modelCalls,
      toolCalls: state.toolCalls,
      usage: state.usage,
      editSucceeded: state.editSucceeded,
      testSucceeded: state.testSucceeded,
    });
    await handle.dispose();
    handle = undefined;
    closeEventLedger();
    exit(completed ? 0 : 1);
  } catch (error) {
    emit({
      type: "agent_error",
      runId: state.runId,
      error: error instanceof Error
        ? { name: error.name, message: error.message }
        : { name: "Error", message: String(error) },
    });
    try { await handle?.dispose(); } catch {}
    closeEventLedger();
    exit(1);
  }
}

export function apply(ctx) {
  eventLedger = openSync(requiredEnv("CI_ADAPTER_EVENT_LEDGER"), "ax", 0o600);
  const state = {
    runId: requiredEnv("CI_ADAPTER_RUN_ID"),
    stage: resolve(requiredEnv("CI_ADAPTER_STAGE")),
    objective: requiredEnv("CI_ADAPTER_OBJECTIVE"),
    readable: stringArray("CI_ADAPTER_READABLE_JSON"),
    mutable: stringArray("CI_ADAPTER_MUTABLE_JSON"),
    testCommand: commandArray("CI_ADAPTER_TEST_COMMAND_JSON"),
    testTimeoutMs: positiveInteger("CI_ADAPTER_TEST_TIMEOUT_MS", 300_000, 300_000),
    maxTurns: positiveInteger("CI_ADAPTER_MAX_TURNS", 32, 64),
    maxToolCalls: positiveInteger("CI_ADAPTER_MAX_TOOL_CALLS", 96, 256),
    maxEditCalls: positiveInteger("CI_ADAPTER_MAX_EDIT_CALLS", 48, 256),
    maxTestCalls: positiveInteger("CI_ADAPTER_MAX_TEST_CALLS", 8, 256),
    maxOutputTokens: positiveInteger("CI_ADAPTER_MAX_OUTPUT_TOKENS", 8192, 65_536),
    modelCalls: 0,
    toolCalls: 0,
    editCalls: 0,
    testCalls: 0,
    editSucceeded: false,
    testSucceeded: false,
    editGeneration: 0,
    toolOutcomes: new Map(),
    requestAttempts: new Map(),
    callIds: new Set(),
    effectIds: new Set(),
    usage: { input: 0, output: 0, total: 0 },
  };
  installTools(ctx, state);
  installGuards(ctx, state);
  run(ctx, state).catch((error) => {
    emit({
      type: "agent_error",
      runId: state.runId,
      error: { name: "Error", message: String(error) },
    });
    closeEventLedger();
    ctx.get("appExit")?.(1);
  });
}
