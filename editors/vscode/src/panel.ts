/** Webview panels showing generated pipeview reports. One panel per
 * report file, reused (content refreshed) on regeneration. */

import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

const panels = new Map<string, vscode.WebviewPanel>();

export async function showReportPanel(htmlPath: string): Promise<void> {
  const key = path.resolve(htmlPath);
  const content = await fs.promises.readFile(key, "utf8");
  const existing = panels.get(key);
  if (existing) {
    existing.webview.html = content;
    existing.reveal(undefined, true);
    return;
  }
  const panel = vscode.window.createWebviewPanel(
    "pipeviewReport",
    path.basename(key).replace(/\.report\.html$/, ""),
    vscode.ViewColumn.Beside,
    {
      // The report is a single self-contained file: inline scripts and
      // styles, zero external fetches (enforced by pipeview's own offline
      // test) — nothing needs localResourceRoots.
      enableScripts: true,
      retainContextWhenHidden: true,
    },
  );
  panel.webview.html = content;
  panel.onDidDispose(() => panels.delete(key));
  panels.set(key, panel);
}
