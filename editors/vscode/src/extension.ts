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

const TOKEN_SECRETS = {
  gitlab: { secret: "pipeview.gitlabToken", env: "PIPEVIEW_GITLAB_TOKEN" },
  github: { secret: "pipeview.githubToken", env: "PIPEVIEW_GITHUB_TOKEN" },
} as const;

type Provider = keyof typeof TOKEN_SECRETS;

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
  for (const { secret, env: name } of Object.values(TOKEN_SECRETS)) {
    if (!env[name]) {
      const stored = await context.secrets.get(secret);
      if (stored) {
        env[name] = stored;
      }
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
  provider: Provider = "gitlab",
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
    const name = provider === "github" ? "GitHub" : "GitLab";
    const action = await vscode.window.showWarningMessage(
      `pipeview needs ${name} access (host and/or API token) configured.`,
      "Authenticate in Terminal",
      `Set ${name} Token`,
      "Show Output",
    );
    if (action === "Authenticate in Terminal") {
      void vscode.commands.executeCommand(`pipeview.${provider}Auth`);
    } else if (action === `Set ${name} Token`) {
      void vscode.commands.executeCommand(
        provider === "github" ? "pipeview.setGitHubToken"
                              : "pipeview.setGitLabToken");
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

  const registerProvider = (
    provider: Provider,
    ids: { report: string; sync: string; auth: string;
           setToken: string; clearToken: string },
    placeholder: string,
  ) => {
    const name = provider === "github" ? "GitHub" : "GitLab";
    register(ids.report, async () => {
      const entry = await vscode.window.showInputBox({
        title: `Pipeview: ${name} project`,
        prompt: `${placeholder.split("@")[0]} — append @ref to pin a branch/tag`,
        placeHolder: placeholder,
        validateInput: (v) => (v.trim() ? undefined : "Enter a project path"),
      });
      if (!entry) {
        return;
      }
      const cwd = pickWorkspaceFolder()?.uri.fsPath ?? process.cwd();
      const args = [provider, "report", entry.trim(), "-o", outDir(context),
                    "--format", "html,json"];
      const invoke = () =>
        runAndShow(context, args, cwd,
                   `pipeview: fetching ${entry.trim()}…`, provider);
      lastInvocation = invoke;
      await invoke();
    });

    register(ids.sync, async () => {
      const cwd = pickWorkspaceFolder()?.uri.fsPath ?? process.cwd();
      const args = [provider, "sync", "-o", outDir(context),
                    "--format", "html,json"];
      const invoke = () =>
        runAndShow(context, args, cwd,
                   "pipeview: syncing tracked projects…", provider);
      lastInvocation = invoke;
      await invoke();
    });

    register(ids.auth, async () => {
      const { providerAuthTerminal } = await import("./gitlab");
      try {
        await providerAuthTerminal(await cli(), provider);
      } catch (e) {
        void vscode.window.showErrorMessage((e as Error).message);
      }
    });

    register(ids.setToken, async () => {
      const token = await vscode.window.showInputBox({
        title: `${name} API token`,
        prompt:
          "Stored in VS Code secret storage and passed to pipeview as " +
          `${TOKEN_SECRETS[provider].env}. Create one via the Pipeview: ` +
          `${name}: Authenticate command or your ${name} settings.`,
        password: true,
        ignoreFocusOut: true,
      });
      if (token) {
        await context.secrets.store(TOKEN_SECRETS[provider].secret,
                                    token.trim());
        void vscode.window.showInformationMessage("pipeview: token stored.");
      }
    });

    register(ids.clearToken, async () => {
      await context.secrets.delete(TOKEN_SECRETS[provider].secret);
      void vscode.window.showInformationMessage(
        "pipeview: stored token cleared.");
    });
  };

  registerProvider("gitlab", {
    report: "pipeview.gitlabReport",
    sync: "pipeview.gitlabSync",
    auth: "pipeview.gitlabAuth",
    setToken: "pipeview.setGitLabToken",
    clearToken: "pipeview.clearGitLabToken",
  }, "group/app@main");

  registerProvider("github", {
    report: "pipeview.githubReport",
    sync: "pipeview.githubSync",
    auth: "pipeview.githubAuth",
    setToken: "pipeview.setGitHubToken",
    clearToken: "pipeview.clearGitHubToken",
  }, "owner/repo@main");
}

export function deactivate(): void {
  // Panels and the output channel are disposed via context.subscriptions.
}
