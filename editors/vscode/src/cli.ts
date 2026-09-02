/**
 * Locating and running the pipeview CLI. This module never imports
 * 'vscode' so its pure helpers run under plain `node --test`.
 */

import { ChildProcessWithoutNullStreams, spawn } from "child_process";

export interface CliCommand {
  /** Executable to spawn. */
  command: string;
  /** Arguments that select pipeview itself (e.g. ["-m", "pipeview"]). */
  prefix: string[];
  /** Human description of how the CLI was found. */
  source: string;
}

export interface RunResult {
  code: number;
  stdout: string;
  stderr: string;
}

export interface ReportOptions {
  useUpstream: boolean;
  upstreamRemote: string;
  extraArgs: string[];
}

/** Argument vector for a repo/file report (everything after the CLI). */
export function buildReportArgs(
  target: string,
  outDir: string,
  opts: ReportOptions,
): string[] {
  const args = [target, "-o", outDir, "--format", "html,json"];
  if (opts.useUpstream) {
    args.push("--upstream");
    if (opts.upstreamRemote) {
      args.push("--upstream-remote", opts.upstreamRemote);
    }
  }
  return args.concat(opts.extraArgs);
}

/**
 * Report HTML paths named by pipeview's stdout. Recognizes all three
 * spellings the CLIs print:
 *   Report generated: <outdir>/<basename>.*          (pipeview <path>)
 *   Report generated: <path>.report.html             (pipeview gitlab report)
 *   <entry>: <path>.report.html [warning]            (pipeview gitlab sync)
 *   rollup: <path>.report.html (<n> projects, …)     (sync rollup)
 */
export function reportHtmlPaths(stdout: string): string[] {
  const out: string[] = [];
  for (const line of stdout.split(/\r?\n/)) {
    let m = /^Report generated: (.+)\.\*$/.exec(line);
    if (m) {
      out.push(`${m[1]}.report.html`);
      continue;
    }
    m = /^Report generated: (.+\.report\.html)$/.exec(line);
    if (m) {
      out.push(m[1]);
      continue;
    }
    m = /^rollup: (.+\.report\.html) \(/.exec(line);
    if (m) {
      out.push(m[1]);
      continue;
    }
    m = /^\S+: (.+\.report\.html)(?: \[\w+\])?$/.exec(line);
    if (m) {
      out.push(m[1]);
    }
  }
  return [...new Set(out)];
}

/** Does stderr carry --upstream's "no API token" degradation notice? */
export function needsTokenHint(stderr: string): boolean {
  return /--upstream: no API token/.test(stderr);
}

/** Does stderr say the gitlab subcommand has no host/token configured? */
export function needsGitLabSetupHint(stderr: string): boolean {
  return /No GitLab host configured|No API token for/.test(stderr);
}

export function defaultPython(platform: NodeJS.Platform): string {
  return platform === "win32" ? "python" : "python3";
}

/** CLI candidates to probe, most preferred first. Every candidate is
 * verified with a `--version` probe before use, so a name that is
 * missing (or, on Windows, the Microsoft Store's "Python was not found"
 * stub) simply falls through to the next one. */
export function cliCandidates(
  cliPath: string,
  pythonPath: string,
  platform: NodeJS.Platform,
): CliCommand[] {
  if (cliPath) {
    return [{ command: cliPath, prefix: [], source: "pipeview.cliPath setting" }];
  }
  const python = pythonPath || defaultPython(platform);
  const candidates: CliCommand[] = [
    { command: "pipeview", prefix: [], source: "pipeview on PATH" },
    {
      command: python,
      prefix: ["-m", "pipeview"],
      source: `${python} -m pipeview`,
    },
  ];
  if (platform === "win32") {
    // The python.org installer leaves python.exe (and pip's Scripts dir,
    // hence pipeview.exe) off PATH by default but always installs the
    // `py` launcher into the Windows directory.
    candidates.push({
      command: "py",
      prefix: ["-3", "-m", "pipeview"],
      source: "py -3 -m pipeview",
    });
  }
  return candidates;
}

/** Windows cannot run .cmd/.bat wrappers as processes, and Node refuses
 * to try (EINVAL since CVE-2024-27980); such a `pipeview.cliPath` (or a
 * python.bat as pythonPath) has to go through cmd.exe. */
export function isWindowsBatchFile(command: string, platform: NodeJS.Platform): boolean {
  return platform === "win32" && /\.(cmd|bat)$/i.test(command);
}

// cmd.exe escaping as done by cross-spawn (lib/util/escape.js), itself
// after https://qntm.org/cmd — the one set of rules known to survive
// both cmd's own parsing and the batch file's re-parsing of its %*.
const CMD_META = /([()\][%!^"`<>&|;, *?])/g;

export function escapeCmdCommand(command: string): string {
  return command.replace(CMD_META, "^$1");
}

export function escapeCmdArgument(arg: string): string {
  // Backslashes before a double quote double up, and the quote is escaped
  // (CRT rules); so do backslashes at the end, which the closing quote
  // would otherwise swallow. Then quote, then caret-escape cmd's meta
  // characters — twice, because the batch file's %* is parsed again.
  let out = arg.replace(/(?=(\\+?)?)\1"/g, '$1$1\\"');
  out = out.replace(/(?=(\\+?)?)\1$/, "$1$1");
  out = `"${out}"`;
  out = out.replace(CMD_META, "^$1");
  return out.replace(CMD_META, "^$1");
}

/** How to spawn a .cmd/.bat with argv: cmd.exe with one pre-escaped
 * command line, passed verbatim. */
export function batchSpawnArgs(
  command: string,
  argv: string[],
  comspec: string | undefined = process.env.ComSpec,
): { file: string; args: string[] } {
  const line = [escapeCmdCommand(command), ...argv.map(escapeCmdArgument)].join(" ");
  return { file: comspec || "cmd.exe", args: ["/d", "/s", "/c", `"${line}"`] };
}

export interface SpawnOptions {
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
}

/** Spawn pipeview with an argv — the one place that knows about
 * .cmd/.bat wrappers, forced UTF-8 and hidden console windows, shared by
 * the one-shot report runs and the long-lived `pipeview lsp` server. */
export function spawnCli(
  cli: CliCommand,
  args: string[],
  options: SpawnOptions = {},
): ChildProcessWithoutNullStreams {
  const platform = options.platform ?? process.platform;
  const argv = [...cli.prefix, ...args];
  const env: NodeJS.ProcessEnv = {
    ...(options.env ?? process.env),
    // Windows Python writes the ANSI code page to pipes; the report
    // paths parsed from stdout are decoded as UTF-8 and a non-ASCII
    // path (C:\Users\José\…) would come out mangled.
    PYTHONUTF8: "1",
  };
  const batch = isWindowsBatchFile(cli.command, platform)
    ? batchSpawnArgs(cli.command, argv)
    : undefined;
  return spawn(batch?.file ?? cli.command, batch?.args ?? argv, {
    cwd: options.cwd,
    env,
    shell: false,
    windowsVerbatimArguments: batch !== undefined,
    // no console window flashing over the editor per run
    windowsHide: true,
  });
}

export function runProcess(
  cli: CliCommand,
  args: string[],
  options: SpawnOptions & { onOutput?: (chunk: string) => void } = {},
): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawnCli(cli, args, options);
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => {
      const text = d.toString();
      stdout += text;
      options.onOutput?.(text);
    });
    child.stderr.on("data", (d: Buffer) => {
      const text = d.toString();
      stderr += text;
      options.onOutput?.(text);
    });
    child.on("error", reject);
    child.on("close", (code) => {
      resolve({ code: code ?? -1, stdout, stderr });
    });
  });
}

/** First candidate whose `--version` probe succeeds. */
export async function locateCli(
  cliPath: string,
  pythonPath: string,
  platform: NodeJS.Platform = process.platform,
): Promise<CliCommand> {
  const candidates = cliCandidates(cliPath, pythonPath, platform);
  const failures: string[] = [];
  for (const candidate of candidates) {
    try {
      const probe = await runProcess(candidate, ["--version"], { platform });
      if (probe.code === 0 && /pipeview/.test(probe.stdout)) {
        return candidate;
      }
      failures.push(`${candidate.source}: exit ${probe.code}`);
    } catch (e) {
      failures.push(`${candidate.source}: ${(e as Error).message}`);
    }
  }
  const install = platform === "win32"
    ? "py -m pip install . from the repository"
    : "pip install . from the repository, or pipx install .";
  throw new Error(
    `pipeview CLI not found. Install it (${install}) or set ` +
      "pipeview.cliPath / pipeview.pythonPath. Tried — " +
      failures.join("; "),
  );
}
