/** Provider flows that need a real TTY: `pipeview gitlab auth` and
 * `pipeview github auth` walk the user through the provider's prefilled
 * token form and a hidden-input paste, so they run in an integrated
 * terminal rather than a child process. */

import * as vscode from "vscode";

import { CliCommand } from "./cli";

export type Provider = "gitlab" | "github";

function shellQuote(part: string): string {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(part) ? part : `'${part.replace(/'/g, "'\\''")}'`;
}

export async function providerAuthTerminal(
  cli: CliCommand,
  provider: Provider,
): Promise<void> {
  const terminal = vscode.window.createTerminal({ name: `pipeview ${provider} auth` });
  terminal.show();
  const cmd = [cli.command, ...cli.prefix, provider, "auth"]
    .map(shellQuote)
    .join(" ");
  terminal.sendText(cmd, true);
}
