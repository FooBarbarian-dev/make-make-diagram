/** The `pipeview lsp` client: inline diagnostics, hover docs and
 * document links come straight from the server; its report code
 * actions are redirected to the webview commands (see lsp.ts). */

import * as vscode from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

import { CliCommand, spawnCli } from "./cli";
import {
  CLIENT_REPORT_COMMAND,
  ServerSettings,
  initializationOptions,
  reportActionFor,
  serverArgs,
} from "./lsp";

/** Buffers the server is told about. It stays silent on YAML that
 * belongs to no pipeline root, so plain `yaml` is safe to include;
 * `github-actions-workflow` is the language id the GitHub Actions
 * extension assigns to workflow files. */
export const DOCUMENT_SELECTOR = [
  { scheme: "file", language: "yaml" },
  { scheme: "file", language: "github-actions-workflow" },
  { scheme: "file", language: "makefile" },
];

export interface ClientParams {
  cli: CliCommand;
  env: NodeJS.ProcessEnv;
  cwd: string | undefined;
  settings: ServerSettings;
  outputDir: string;
  output: vscode.OutputChannel;
}

export function createClient(p: ClientParams): LanguageClient {
  // A function rather than an Executable so the spawn goes through the
  // same path as report runs (.cmd wrappers via cmd.exe, PYTHONUTF8,
  // hidden console window).
  const serverOptions: ServerOptions = () =>
    Promise.resolve(spawnCli(p.cli, serverArgs(p.cli), { env: p.env, cwd: p.cwd }));
  const clientOptions: LanguageClientOptions = {
    documentSelector: DOCUMENT_SELECTOR,
    outputChannel: p.output,
    initializationOptions: initializationOptions(p.settings, p.outputDir),
    middleware: {
      provideCodeActions: async (document, range, context, token, next) => {
        const actions = await next(document, range, context, token);
        return (actions ?? []).map((a) => redirectReportAction(a, document.uri));
      },
    },
  };
  return new LanguageClient("pipeview", "Pipeview", serverOptions, clientOptions);
}

function redirectReportAction(
  action: vscode.Command | vscode.CodeAction,
  uri: vscode.Uri,
): vscode.Command | vscode.CodeAction {
  const isCodeAction = action instanceof vscode.CodeAction;
  const rewrite = reportActionFor(
    isCodeAction ? action.command?.command : (action as vscode.Command).command,
  );
  if (!rewrite) {
    return action;
  }
  const command: vscode.Command = {
    title: rewrite.title,
    command: CLIENT_REPORT_COMMAND,
    arguments: [uri, { upstream: rewrite.upstream }],
  };
  if (!isCodeAction) {
    return command;
  }
  const replacement = new vscode.CodeAction(rewrite.title, action.kind);
  replacement.command = command;
  return replacement;
}
