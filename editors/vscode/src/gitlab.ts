/** GitLab flows that need a real TTY: `pipeview gitlab auth` walks the
 * user through GitLab's prefilled token form and a hidden-input paste,
 * so it runs in an integrated terminal rather than a child process. */

import * as vscode from "vscode";

import { CliCommand } from "./cli";

function shellQuote(part: string): string {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(part) ? part : `'${part.replace(/'/g, "'\\''")}'`;
}

export async function gitlabAuthTerminal(cli: CliCommand): Promise<void> {
  const terminal = vscode.window.createTerminal({ name: "pipeview auth" });
  terminal.show();
  const cmd = [cli.command, ...cli.prefix, "gitlab", "auth"]
    .map(shellQuote)
    .join(" ");
  terminal.sendText(cmd, true);
}
