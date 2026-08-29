import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

import {
  CliCommand,
  buildReportArgs,
  locateCli,
  needsGitLabSetupHint,
  needsTokenHint,
  reportHtmlPaths,
  runProcess,
} from "./cli";
import { showReportPanel } from "./panel";

const TOKEN_SECRET = "pipeview.gitlabToken";

let output: vscode.OutputChannel;
let cachedCli: CliCommand | undefined;
let lastInvocation: (() => Promise<void>) | undefined;

interface Settings {
  pythonPath: string;
  cliPath: string;
  outputDirectory: string;
  useUpstream: boolean;
  upstreamRemote: string;
  extraArgs: string[];
}

function settings(): Settings {
  const cfg = vscode.workspace.getConfiguration("pipeview");
  return {
    pythonPath: cfg.get("pythonPath", ""),
    cliPath: cfg.get("cliPath", ""),
    outputDirectory: cfg.get("outputDirectory", ""),
    useUpstream: cfg.get("useUpstream", true),
    upstreamRemote: cfg.get("upstreamRemote", ""),
    extraArgs: cfg.get("extraArgs", []),
  };
}

async function cli(): Promise<CliCommand> {
  if (!cachedCli) {
    const s = settings();
    cachedCli = await locateCli(s.cliPath, s.pythonPath);
    output.appendLine(`[pipeview] using ${cachedCli.source}`);
  }
  return cachedCli;
}

function outDir(context: vscode.ExtensionContext): string {
  const configured = settings().outputDirectory;
  if (configured) {
    return configured;
  }
  const base = context.storageUri ?? context.globalStorageUri;
  return path.join(base.fsPath, "reports");
}

function pickWorkspaceFolder(): vscode.WorkspaceFolder | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    return undefined;
  }
  const active = vscode.window.activeTextEditor?.document.uri;
  if (active) {
    const owner = vscode.workspace.getWorkspaceFolder(active);
    if (owner) {
      return owner;
    }
  }
  return folders[0];
}

async function spawnEnv(
  context: vscode.ExtensionContext,
): Promise<NodeJS.ProcessEnv> {
  const env = { ...process.env };
  if (!env.PIPEVIEW_GITLAB_TOKEN) {
    const stored = await context.secrets.get(TOKEN_SECRET);
    if (stored) {
      env.PIPEVIEW_GITLAB_TOKEN = stored;
    }
  }
  return env;
}

/** Run pipeview, stream output to the channel, open every report it
 * names, and surface the right follow-up (token/setup hints). */
async function runAndShow(
  context: vscode.ExtensionContext,
  args: string[],
  cwd: string,
  title: string,
): Promise<void> {
  let command: CliCommand;
  try {
    command = await cli();
  } catch (e) {
    void vscode.window.showErrorMessage((e as Error).message);
    return;
  }
  output.appendLine(
    `[pipeview] ${command.command} ${[...command.prefix, ...args].join(" ")}`,
  );
  const env = await spawnEnv(context);
  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title },
    () =>
      runProcess(command, args, { cwd, env, onOutput: (t) => output.append(t) }),
  );

  const reports = reportHtmlPaths(result.stdout).filter((p) => fs.existsSync(p));
  for (const report of reports) {
    await showReportPanel(report);
  }

  if (needsGitLabSetupHint(result.stderr)) {
    const action = await vscode.window.showWarningMessage(
      "pipeview needs GitLab access (host and/or API token) configured.",
      "Authenticate in Terminal",
      "Set GitLab Token",
      "Show Output",
    );
    if (action === "Authenticate in Terminal") {
      void vscode.commands.executeCommand("pipeview.gitlabAuth");
    } else if (action === "Set GitLab Token") {
      void vscode.commands.executeCommand("pipeview.setGitLabToken");
    } else if (action === "Show Output") {
      output.show();
    }
    return;
  }

  if (reports.length === 0) {
    const action = await vscode.window.showErrorMessage(
      `pipeview produced no report (exit ${result.code}).`,
      "Show Output",
    );
    if (action) {
      output.show();
    }
    return;
  }

  if (needsTokenHint(result.stderr)) {
    const action = await vscode.window.showWarningMessage(
      "Cross-repository includes stay unresolved: no GitLab API token for " +
        "the upstream host.",
      "Set GitLab Token",
      "Show Output",
    );
    if (action === "Set GitLab Token") {
      void vscode.commands.executeCommand("pipeview.setGitLabToken");
    } else if (action === "Show Output") {
      output.show();
    }
  } else if (result.code !== 0) {
    const action = await vscode.window.showWarningMessage(
      "Report generated with diagnostics — see its Files tab or the output.",
      "Show Output",
    );
    if (action) {
      output.show();
    }
  }
}

async function reportOn(
  context: vscode.ExtensionContext,
  target: string,
  cwd: string,
): Promise<void> {
  const s = settings();
  const args = buildReportArgs(target, outDir(context), {
    useUpstream: s.useUpstream,
    upstreamRemote: s.upstreamRemote,
    extraArgs: s.extraArgs,
  });
  const invoke = () =>
    runAndShow(context, args, cwd, `pipeview: analyzing ${path.basename(target)}…`);
  lastInvocation = invoke;
  await invoke();
}

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("Pipeview");
  context.subscriptions.push(output);
  vscode.workspace.onDidChangeConfiguration(
    (e) => {
      if (e.affectsConfiguration("pipeview")) {
        cachedCli = undefined;
      }
    },
    undefined,
    context.subscriptions,
  );

  const register = (id: string, fn: (...a: unknown[]) => unknown) =>
    context.subscriptions.push(vscode.commands.registerCommand(id, fn));

  register("pipeview.showReport", async () => {
    const folder = pickWorkspaceFolder();
    if (!folder) {
      void vscode.window.showErrorMessage("pipeview: open a folder first.");
      return;
    }
    await reportOn(context, folder.uri.fsPath, folder.uri.fsPath);
  });

  register("pipeview.showReportForFile", async (resource) => {
    const uri =
      resource instanceof vscode.Uri
        ? resource
        : vscode.window.activeTextEditor?.document.uri;
    if (!uri || uri.scheme !== "file") {
      void vscode.window.showErrorMessage(
        "pipeview: no file selected (save the file to disk first).",
      );
      return;
    }
    await reportOn(context, uri.fsPath, path.dirname(uri.fsPath));
  });

  register("pipeview.refreshReport", async () => {
    if (!lastInvocation) {
      void vscode.commands.executeCommand("pipeview.showReport");
      return;
    }
    await lastInvocation();
  });

  register("pipeview.gitlabReport", async () => {
    const entry = await vscode.window.showInputBox({
      title: "Pipeview: GitLab project",
      prompt: "group/project, or group/project@ref to pin a branch/tag",
      placeHolder: "group/app@main",
      validateInput: (v) => (v.trim() ? undefined : "Enter a project path"),
    });
    if (!entry) {
      return;
    }
    const cwd = pickWorkspaceFolder()?.uri.fsPath ?? process.cwd();
    const args = ["gitlab", "report", entry.trim(), "-o", outDir(context),
                  "--format", "html,json"];
    const invoke = () =>
      runAndShow(context, args, cwd, `pipeview: fetching ${entry.trim()}…`);
    lastInvocation = invoke;
    await invoke();
  });

  register("pipeview.gitlabSync", async () => {
    const cwd = pickWorkspaceFolder()?.uri.fsPath ?? process.cwd();
    const args = ["gitlab", "sync", "-o", outDir(context), "--format", "html,json"];
    const invoke = () =>
      runAndShow(context, args, cwd, "pipeview: syncing tracked projects…");
    lastInvocation = invoke;
    await invoke();
  });

  register("pipeview.gitlabAuth", async () => {
    const { gitlabAuthTerminal } = await import("./gitlab");
    try {
      await gitlabAuthTerminal(await cli());
    } catch (e) {
      void vscode.window.showErrorMessage((e as Error).message);
    }
  });

  register("pipeview.setGitLabToken", async () => {
    const token = await vscode.window.showInputBox({
      title: "GitLab API token (read_api scope)",
      prompt:
        "Stored in VS Code secret storage and passed to pipeview as " +
        "PIPEVIEW_GITLAB_TOKEN. Create one via the Pipeview: GitLab: " +
        "Authenticate command or your GitLab profile settings.",
      password: true,
      ignoreFocusOut: true,
    });
    if (token) {
      await context.secrets.store(TOKEN_SECRET, token.trim());
      void vscode.window.showInformationMessage("pipeview: token stored.");
    }
  });

  register("pipeview.clearGitLabToken", async () => {
    await context.secrets.delete(TOKEN_SECRET);
    void vscode.window.showInformationMessage("pipeview: stored token cleared.");
  });
}

export function deactivate(): void {
  // Panels and the output channel are disposed via context.subscriptions.
}
