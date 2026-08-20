import { lstat, mkdir, readFile, realpath, readdir, stat, writeFile } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";

type PiApi = {
  registerTool(tool: Record<string, unknown>): void;
  on(name: string, handler: (...args: any[]) => unknown): void;
  setActiveTools(names: string[]): void;
  exec(command: string, args: string[], options: Record<string, unknown>): Promise<{
    stdout: string;
    stderr: string;
    code: number;
    killed: boolean;
  }>;
};

const stage = resolve(requiredEnv("CI_ADAPTER_STAGE"));
const readable = parseStringArray("CI_ADAPTER_READABLE_JSON");
const mutable = parseStringArray("CI_ADAPTER_MUTABLE_JSON");
const testCommand = parseCommandArray("CI_ADAPTER_TEST_COMMAND_JSON");
const maxToolCalls = parsePositiveInteger("CI_ADAPTER_MAX_TOOL_CALLS", 12);
const testTimeoutMs = parsePositiveInteger("CI_ADAPTER_TEST_TIMEOUT_MS", 300000);
let toolCalls = 0;
let editCalls = 0;
let testCalls = 0;

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function parseStringArray(name: string): string[] {
  const value = JSON.parse(requiredEnv(name));
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string" && item.length > 0)) {
    throw new Error(`${name} must be a non-empty-string JSON array`);
  }
  return [...new Set(value.map(normalizeRelative))];
}

function parseCommandArray(name: string): string[] {
  const value = JSON.parse(requiredEnv(name));
  if (!Array.isArray(value) || value.length === 0 || !value.every((item) => typeof item === "string" && item.length > 0 && !item.includes("\0"))) {
    throw new Error(`${name} must be a non-empty argv JSON array`);
  }
  return value;
}

function parsePositiveInteger(name: string, fallback: number): number {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isSafeInteger(value) || value < 1) throw new Error(`${name} must be a positive integer`);
  return value;
}

function normalizeRelative(raw: string): string {
  const text = raw.replaceAll("\\", "/");
  const parts = text.split("/");
  if (!text || isAbsolute(raw) || parts.some((part) => !part || part === "." || part === "..")) {
    throw new Error(`unsafe relative path: ${JSON.stringify(raw)}`);
  }
  return parts.join("/");
}

function isAllowed(path: string, roots: string[]): boolean {
  const normalized = normalizeRelative(path);
  return roots.some((root) => normalized === root || normalized.startsWith(`${root}/`));
}

async function scopedPath(raw: string, roots: string[], requireExisting = true): Promise<string> {
  if (!isAllowed(raw, roots)) throw new Error(`path is outside declared scope: ${JSON.stringify(raw)}`);
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
      if (info.isSymbolicLink()) throw new Error(`symbolic links are forbidden: ${raw}`);
    } catch (error: any) {
      if (error?.code === "ENOENT") break;
      throw error;
    }
  }
  if (requireExisting) {
    const canonicalStage = await realpath(stage);
    const canonicalTarget = await realpath(target);
    const realRel = relative(canonicalStage, canonicalTarget);
    if (realRel === ".." || realRel.startsWith(`..${sep}`) || isAbsolute(realRel)) {
      throw new Error(`real path escapes stage: ${JSON.stringify(raw)}`);
    }
  }
  return target;
}

async function collectFiles(target: string, roots: string[], output: Set<string>): Promise<void> {
  const info = await stat(target);
  if (info.isFile()) {
    output.add(target);
    return;
  }
  if (!info.isDirectory()) return;
  for (const item of await readdir(target, { withFileTypes: true })) {
    const child = resolve(target, item.name);
    const rel = relative(stage, child).split(sep).join("/");
    if (!isAllowed(rel, roots)) continue;
    if (item.isSymbolicLink()) throw new Error(`symbolic links are forbidden: ${rel}`);
    if (item.isDirectory()) await collectFiles(child, roots, output);
    else if (item.isFile()) output.add(child);
  }
}

const stringSchema = { type: "string" };

export default function scopedTools(pi: PiApi): void {
  pi.registerTool({
    name: "read",
    label: "Read staged file",
    description: "Read one declared model-readable file from the disposable stage.",
    promptSnippet: "Read a declared staged file",
    parameters: {
      type: "object",
      properties: { path: stringSchema },
      required: ["path"],
      additionalProperties: false,
    },
    async execute(_id: string, params: { path: string }) {
      const target = await scopedPath(params.path, readable);
      const info = await stat(target);
      if (!info.isFile()) throw new Error(`read target is not a file: ${params.path}`);
      if (info.size > 262144) throw new Error(`read target exceeds 256 KiB: ${params.path}`);
      const text = await readFile(target, "utf8");
      return { content: [{ type: "text", text }], details: { path: normalizeRelative(params.path), bytes: info.size } };
    },
  });

  pi.registerTool({
    name: "search",
    label: "Search staged files",
    description: "Literal case-sensitive search over declared model-readable staged files.",
    promptSnippet: "Search declared staged files",
    parameters: {
      type: "object",
      properties: { query: stringSchema, path: stringSchema },
      required: ["query"],
      additionalProperties: false,
    },
    async execute(_id: string, params: { query: string; path?: string }) {
      if (!params.query || params.query.length > 512) throw new Error("search query must be 1..512 characters");
      const files = new Set<string>();
      const roots = params.path ? [normalizeRelative(params.path)] : readable;
      if (params.path && !isAllowed(params.path, readable)) throw new Error("search path is outside readable scope");
      for (const root of roots) {
        const target = await scopedPath(root, readable);
        await collectFiles(target, readable, files);
      }
      const matches: string[] = [];
      for (const file of [...files].sort()) {
        if ((await stat(file)).size > 262144) continue;
        let text: string;
        try { text = await readFile(file, "utf8"); } catch { continue; }
        const rel = relative(stage, file).split(sep).join("/");
        for (const [index, line] of text.split(/\r?\n/).entries()) {
          if (line.includes(params.query)) matches.push(`${rel}:${index + 1}:${line.slice(0, 500)}`);
          if (matches.length >= 200) break;
        }
        if (matches.length >= 200) break;
      }
      return {
        content: [{ type: "text", text: matches.join("\n") || "No matches" }],
        details: { query: params.query, matches: matches.length, truncated: matches.length >= 200 },
      };
    },
  });

  pi.registerTool({
    name: "edit",
    label: "Edit staged file",
    description: "Perform one exact-text replacement in a declared mutable staged file.",
    promptSnippet: "Make one scoped exact-text edit",
    parameters: {
      type: "object",
      properties: {
        path: stringSchema,
        edits: {
          type: "array",
          minItems: 1,
          maxItems: 1,
          items: {
            type: "object",
            properties: { oldText: stringSchema, newText: stringSchema },
            required: ["oldText", "newText"],
            additionalProperties: false,
          },
        },
      },
      required: ["path", "edits"],
      additionalProperties: false,
    },
    async execute(_id: string, params: { path: string; edits: Array<{ oldText: string; newText: string }> }) {
      if (!Array.isArray(params.edits) || params.edits.length !== 1) throw new Error("edit requires exactly one replacement block");
      const { oldText, newText } = params.edits[0];
      if (newText.length > 524288) throw new Error("replacement exceeds 512 KiB");
      const normalized = normalizeRelative(params.path);
      const target = await scopedPath(normalized, mutable, false);
      let original: string;
      try {
        original = await readFile(target, "utf8");
      } catch (error: any) {
        if (error?.code !== "ENOENT" || oldText !== "") throw error;
        await mkdir(resolve(target, ".."), { recursive: true });
        await writeFile(target, newText, { encoding: "utf8", flag: "wx" });
        return { content: [{ type: "text", text: `Created ${normalized}` }], details: { path: normalized, created: true } };
      }
      const occurrences = oldText === "" ? 0 : original.split(oldText).length - 1;
      if (occurrences !== 1) throw new Error(`old_text must occur exactly once; found ${occurrences}`);
      const updated = original.replace(oldText, newText);
      await writeFile(target, updated, "utf8");
      return { content: [{ type: "text", text: `Updated ${normalized}` }], details: { path: normalized, created: false } };
    },
  });

  pi.registerTool({
    name: "test",
    label: "Run frozen verifier",
    description: "Run only the task capsule's exact argv verifier in the disposable stage.",
    promptSnippet: "Run the frozen verifier",
    parameters: { type: "object", properties: {}, additionalProperties: false },
    async execute(_id: string, _params: Record<string, never>, signal: AbortSignal) {
      const result = await pi.exec(testCommand[0], testCommand.slice(1), {
        cwd: stage,
        signal,
        timeout: testTimeoutMs,
      });
      const stdout = result.stdout.slice(-16384);
      const stderr = result.stderr.slice(-16384);
      return {
        content: [{ type: "text", text: `exit=${result.code}\n${stdout}\n${stderr}`.slice(-32768) }],
        details: { command: testCommand, exitCode: result.code, killed: result.killed, stdout, stderr },
      };
    },
  });

  pi.on("session_start", () => {
    pi.setActiveTools(["read", "search", "edit", "test"]);
  });
  pi.on("session_before_compact", () => ({ cancel: true }));
  pi.on("tool_call", (event: { toolName: string; input: Record<string, unknown> }) => {
    toolCalls += 1;
    if (toolCalls > maxToolCalls) return { block: true, reason: "tool-call budget exceeded", terminate: true };
    if (!["read", "search", "edit", "test"].includes(event.toolName)) {
      return { block: true, reason: "tool is outside the qualification allowlist", terminate: true };
    }
    if (event.toolName === "edit" && ++editCalls > 1) {
      return { block: true, reason: "qualification allows one edit call", terminate: true };
    }
    if (event.toolName === "test" && ++testCalls > 1) {
      return { block: true, reason: "qualification allows one test call", terminate: true };
    }
    const path = event.input?.path;
    if (event.toolName === "edit" && (typeof path !== "string" || !isAllowed(path, mutable))) {
      return { block: true, reason: "edit path is outside mutable scope", terminate: true };
    }
    if (event.toolName === "read" && (typeof path !== "string" || !isAllowed(path, readable))) {
      return { block: true, reason: "read path is outside readable scope", terminate: true };
    }
    if (event.toolName === "search" && path !== undefined && (typeof path !== "string" || !isAllowed(path, readable))) {
      return { block: true, reason: "search path is outside readable scope", terminate: true };
    }
  });
}
