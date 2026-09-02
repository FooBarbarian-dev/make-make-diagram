//! Zed extension for pipeview: wires the `pipeview lsp` language server
//! up for YAML and Make buffers. Everything of substance lives in the
//! server (see pipeview/lsp.py) — this extension only finds the binary
//! and forwards the user's settings, per editors/README.md.
//!
//! Zed applies the `lsp.pipeview.binary` setting itself, before this
//! extension is consulted (crates/project/src/lsp_store.rs): a
//! configured `path` is run with the configured `arguments` verbatim —
//! an empty list when unset, so no implicit `lsp` — and configured
//! `arguments` without a `path` replace whatever `language_server_command`
//! returns. Hence nothing here reads `binary`: the documented escape
//! hatch is `pipeview-lsp` (the server as its own executable) or
//! `pipeview` with `"arguments": ["lsp"]`.

use zed_extension_api::{
    self as zed, settings::LspSettings, Command, LanguageServerId, Os, Result, Worktree,
};

struct PipeviewExtension;

/// Interpreter fallbacks for `python -m pipeview lsp` — (executable,
/// arguments) — most trustworthy first. A WebAssembly extension cannot
/// probe `--version`, so this order is the only defense against a wrong
/// hit:
///
/// - Windows: `py`, the launcher the python.org installer always puts on
///   PATH (unlike python.exe and pip's Scripts directory, which the
///   installer leaves off PATH by default) and the one name the Microsoft
///   Store never shadows with its "Python was not found" stub; then
///   `python`, which is real when the PATH box was ticked or Python came
///   from the Store; `python3` last — python.org installs never create it,
///   while the Store stub answers to it.
/// - elsewhere: `python3`, then `python`.
fn interpreters(os: Os) -> &'static [(&'static str, &'static [&'static str])] {
    const MODULE: &[&str] = &["-m", "pipeview", "lsp"];
    const LAUNCHER: &[&str] = &["-3", "-m", "pipeview", "lsp"];
    match os {
        Os::Windows => &[("py", LAUNCHER), ("python", MODULE), ("python3", MODULE)],
        _ => &[("python3", MODULE), ("python", MODULE)],
    }
}

fn not_found_message(os: Os) -> String {
    // Zed runs a configured binary path with no arguments unless
    // `arguments` is set too, so the example names the server's own
    // executable rather than `pipeview` (which would need ["lsp"]).
    let (install, example) = match os {
        Os::Windows => ("`py -m pip install .`", r"C:\\path\\to\\Scripts\\pipeview-lsp.exe"),
        _ => ("`pip install .` or `pipx install .`", "/path/to/pipeview-lsp"),
    };
    format!(
        "pipeview not found. Install it ({install} from the make-make-diagram \
         repository), or point Zed at the server in settings.json: \
         {{\"lsp\": {{\"pipeview\": {{\"binary\": {{\"path\": \"{example}\"}}}}}}}} \
         (a path to `pipeview` itself also needs \"arguments\": [\"lsp\"])"
    )
}

fn strings(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|s| s.to_string()).collect()
}

impl zed::Extension for PipeviewExtension {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        _language_server_id: &LanguageServerId,
        worktree: &Worktree,
    ) -> Result<Command> {
        // Only reached when no `binary.path` is configured (see the
        // module docs): this decides the default, and a configured
        // `binary.arguments` overrides the args chosen here.
        //
        // The worktree shell env flows through so GitLab tokens
        // (PIPEVIEW_GITLAB_TOKEN et al.) reach the server's --upstream runs.
        let env = worktree.shell_env();
        let (os, _arch) = zed::current_platform();

        // `which` resolves pipeview.exe on Windows via PATHEXT.
        if let Some(path) = worktree.which("pipeview") {
            return Ok(Command {
                command: path,
                args: vec!["lsp".to_string()],
                env,
            });
        }

        for (python, args) in interpreters(os) {
            if let Some(path) = worktree.which(python) {
                return Ok(Command {
                    command: path,
                    args: strings(args),
                    env,
                });
            }
        }

        Err(not_found_message(os))
    }

    fn language_server_initialization_options(
        &mut self,
        _language_server_id: &LanguageServerId,
        worktree: &Worktree,
    ) -> Result<Option<zed::serde_json::Value>> {
        // Forwarded verbatim; the server documents the keys
        // (upstream, upstreamRemote, outputDir).
        Ok(LspSettings::for_worktree("pipeview", worktree)
            .ok()
            .and_then(|settings| settings.initialization_options))
    }
}

zed::register_extension!(PipeviewExtension);
