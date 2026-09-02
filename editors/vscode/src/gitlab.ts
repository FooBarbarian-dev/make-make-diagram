/** Provider flows that need a real TTY: `pipeview gitlab auth` and
 * `pipeview github auth` walk the user through the provider's prefilled
 * token form and a hidden-input paste, so they run in an integrated
 * terminal rather than a child process. */

import * as vscode from "vscode";

import { CliCommand } from "./cli";

export type Provider = "gitlab" | "github";

/** The terminal's own process is pipeview itself (shellPath/shellArgs),
 * so no shell ever parses the command line: the same argv works under
 * PowerShell-default Windows, cmd, bash, and a Remote-WSL extension host,
 * whatever characters the configured cliPath/pythonPath contain. */
export function authTerminalOptions(
  cli: CliCommand,
  provider: Provider,
): vscode.TerminalOptions {
  return {
    name: `pipeview ${provider} auth`,
    shellPath: cli.command,
    shellArgs: [...cli.prefix, provider, "auth"],
  };
}

export async function providerAuthTerminal(
  cli: CliCommand,
  provider: Provider,
): Promise<void> {
  const terminal = vscode.window.createTerminal(authTerminalOptions(cli, provider));
  terminal.show();
  // VS Code closes a terminal whose process exits cleanly, and with it
  // the CLI's final "token stored" line — say so here instead. A failed
  // run stays open with VS Code's own exit-code alert.
  const closed = vscode.window.onDidCloseTerminal((t) => {
    if (t !== terminal) {
      return;
    }
    closed.dispose();
    if (t.exitStatus?.code === 0) {
      void vscode.window.showInformationMessage(
        `pipeview: ${provider} authentication finished — the token is stored ` +
          "in pipeview's own config and used by every command.",
      );
    }
  });
}
