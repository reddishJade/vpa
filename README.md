# VPA

VPA is an architecture-port promotion tool.

Its primary use case is keeping an unsupported target ISA implementation moving
by translating upstream work from a supported reference ISA into the local target
ISA. The motivating path is:

```text
box64 RISC-V/RISC-family upstream -> SW64 local port
```

This is different from a normal upstream merge. If the target ISA were supported
upstream, Git merge/rebase/cherry-pick would usually be enough. VPA exists for
the case where upstream changes must be interpreted through a reference backend
and applied semantically to a local backend.

See [DESIGN.md](DESIGN.md) for the current architecture.

## Configuration

VPA reads `vpa.toml` from the current directory when it exists. Copy
`vpa.example.toml` to `vpa.toml`, then edit repo paths, validation commands, and
the OpenAI-compatible LLM endpoint once.

Keep API keys in the environment and reference the variable name from config:

```toml
[llm]
model = "your-model"
base_url = "https://your-provider/v1"
api_key_env = "VPA_API_KEY"
```

After that, day-to-day commands can stay short:

```powershell
uv run python -m vpa.main promote `
  --rev-range upstream/main~3..upstream/main `
  --dry-run
```

## Current Status

The previous implementation was a ledger-driven agent harness. The current
design replaces that with a workflow-first architecture:

- Git is the default mechanical promotion engine.
- Reference ISA changes are mapped to target ISA changes.
- Build and tests are the correctness gate.
- LLMs are used only for semantic mapping, conflict resolution, repair, and
  manual-action explanations.

The implementation should be rebuilt around this model rather than optimizing
the old per-file agent tool-call loop.
