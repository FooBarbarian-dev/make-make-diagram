/**
 * `pipeview lsp` from VS Code's side: how the server is started and how
 * its report code actions become this extension's own webview flow.
 * Never imports 'vscode', so the helpers run under plain `node --test`;
 * the client itself is assembled in client.ts.
 */

import { CliCommand } from "./cli";

/** Argument vector that turns the located CLI into the language server. */
export function serverArgs(cli: CliCommand): string[] {
  return [...cli.prefix, "lsp"];
}

export interface ServerSettings {
  useUpstream: boolean;
  upstreamRemote: string;
}

/** initializationOptions as pipeview/lsp.py documents them. */
export function initializationOptions(
  settings: ServerSettings,
  outputDir: string,
): Record<string, unknown> {
  return {
    // The attach toast exists for editors without a command palette
    // (Zed); here the Pipeview commands are discoverable on their own.
    announce: false,
    upstream: settings.useUpstream,
    upstreamRemote: settings.upstreamRemote,
    outputDir,
  };
}

/** The server's report commands open the report in a browser — the
 * right thing in Zed, which has no webviews. VS Code does, so those
 * actions are rewritten to this extension's own command instead. */
export const SERVER_REPORT_COMMANDS: Readonly<Record<string, { upstream: boolean }>> = {
  "pipeview.openReport": { upstream: true },
  "pipeview.openReportOffline": { upstream: false },
};

export const CLIENT_REPORT_COMMAND = "pipeview.showReportForFile";

export interface ReportAction {
  title: string;
  /** false forces a run without `--upstream`; true means "as configured"
   * (the `pipeview.useUpstream` setting still decides). */
  upstream: boolean;
}

/** How a server code action carrying `command` should look client-side,
 * or undefined for anything that is not a report action. */
export function reportActionFor(command: string | undefined): ReportAction | undefined {
  if (!command || !Object.prototype.hasOwnProperty.call(SERVER_REPORT_COMMANDS, command)) {
    return undefined;
  }
  const { upstream } = SERVER_REPORT_COMMANDS[command];
  return {
    title: upstream
      ? "Pipeview: open pipeline report"
      : "Pipeview: open pipeline report without upstream fetch",
    upstream,
  };
}
