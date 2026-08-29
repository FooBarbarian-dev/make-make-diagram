//! Zed extension for pipeview: wires the `pipeview lsp` language server
//! up for YAML and Make buffers. Everything of substance lives in the
//! server (see pipeview/lsp.py) — this extension only finds the binary
//! and forwards the user's settings, per editors/README.md.

use zed_extension_api::{
    self as zed, settings::LspSettings, Command, LanguageServerId, Result, Worktree,
};

struct PipeviewExtension;

impl zed::Extension for PipeviewExtension {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        _language_server_id: &LanguageServerId,
        worktree: &Worktree,
    ) -> Result<Command> {
        // The worktree shell env flows through so GitLab tokens
        // (PIPEVIEW_GITLAB_TOKEN et al.) reach the server's --upstream runs.
        let env = worktree.shell_env();

        // An explicit binary from Zed settings wins:
        //   "lsp": {"pipeview": {"binary": {"path": "...", "arguments": [...]}}}
        if let Some(binary) = LspSettings::for_worktree("pipeview", worktree)
            .ok()
            .and_then(|settings| settings.binary)
        {
            if let Some(path) = binary.path {
                let args = binary.arguments.unwrap_or_else(|| vec!["lsp".to_string()]);
                return Ok(Command {
                    command: path,
                    args,
                    env,
                });
            }
        }

        if let Some(path) = worktree.which("pipeview") {
            return Ok(Command {
                command: path,
                args: vec!["lsp".to_string()],
                env,
            });
        }

        for python in ["python3", "python"] {
            if let Some(path) = worktree.which(python) {
                return Ok(Command {
                    command: path,
                    args: vec!["-m".to_string(), "pipeview".to_string(), "lsp".to_string()],
                    env,
                });
            }
        }

        Err("pipeview not found. Install it (`pip install .` from the \
             make-make-diagram repository, or `pipx install .`), or point Zed \
             at it in settings.json: {\"lsp\": {\"pipeview\": {\"binary\": \
             {\"path\": \"/path/to/pipeview\"}}}}"
            .to_string())
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
