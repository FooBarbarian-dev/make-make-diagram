/**
 * Locating and running the pipeview CLI. This module never imports
 * 'vscode' so its pure helpers run under plain `node --test`.
 */

import { spawn } from "child_process";

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

/** CLI candidates to probe, most preferred first. */
export function cliCandidates(
  cliPath: string,
  pythonPath: string,
  platform: NodeJS.Platform,
): CliCommand[] {
  if (cliPath) {
    return [{ command: cliPath, prefix: [], source: "pipeview.cliPath setting" }];
  }
  const python = pythonPath || defaultPython(platform);
  return [
    { command: "pipeview", prefix: [], source: "pipeview on PATH" },
    {
      command: python,
      prefix: ["-m", "pipeview"],
      source: `${python} -m pipeview`,
    },
  ];
}

export function runProcess(
  cli: CliCommand,
  args: string[],
  options: {
    cwd?: string;
    env?: NodeJS.ProcessEnv;
    onOutput?: (chunk: string) => void;
  } = {},
): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(cli.command, [...cli.prefix, ...args], {
      cwd: options.cwd,
      env: options.env ?? process.env,
      shell: false,
    });
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
      const probe = await runProcess(candidate, ["--version"]);
      if (probe.code === 0 && /pipeview/.test(probe.stdout)) {
        return candidate;
      }
      failures.push(`${candidate.source}: exit ${probe.code}`);
    } catch (e) {
      failures.push(`${candidate.source}: ${(e as Error).message}`);
    }
  }
  throw new Error(
    "pipeview CLI not found. Install it (pip install pipeview / " +
      "pip install . from the repository) or set pipeview.cliPath / " +
      `pipeview.pythonPath. Tried — ${failures.join("; ")}`,
  );
}
