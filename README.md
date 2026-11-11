# deja-fix

**Stops your agent from putting back code that already failed.**

Agents go in circles: try A, the tests fail; try B, they fail; try A again. Each attempt is a slightly different edit, so nothing notices, and you watch the same failure come back twenty minutes later.

Déjà Fix remembers every version of every file the agent writes in a session, and what happened after each one. When an edit would put a file back to a version that already failed, it is stopped before it runs:

```text
Deja Fix: this puts calc.py back exactly as it was at 08:11, and that version already failed
(`python -m pytest -q test_calc.py` exited 1). Try a different approach. If you're sure this
version is right and something else was the cause, say so and try again; it will go through.
```

## How it works

1. **Every version is fingerprinted.** After each successful `write_file` or `patch`, the file on disk is hashed. Trailing spaces and blank lines at the end don't count, so cosmetic changes don't hide a repeat.
2. **Each version gets a verdict:**
   - **A test or build run** marks the files changed since the previous run. A failure means that version failed; exit code 0 means it passed. It reads `pytest`, `npm test`, `cargo test`, `go test`, `tsc` and similar. Commands that couldn't run at all (exit 126/127) give no verdict.
   - **You saying "still broken"**, "same error" or "doesn't work" marks the files the agent changed in its last turn as failed.
3. **Edits are checked before they run.** For `write_file` it's the new content; for `patch`, Déjà Fix applies the patch to the current file to see the result. If that exact version already failed and never passed, the edit is stopped.
4. **The agent can insist.** Sometimes the failure was caused by another file. If the agent makes the same edit again after being told, it goes through, and that's counted.

Hermes's own guard stops *identical tool calls* repeated many times. Déjà Fix catches the *same code* reached by *different* edits, which is how agents actually loop.

## Use it

```text
/deja      files tracked, versions known to fail, edits stopped and let through
```

```yaml
plugins:
  entries:
    deja-fix:
      settings:
        mode: block    # block | warn (record only) | off
```

## What it touches

- **Reads** the files the agent edits, to fingerprint them and to predict patches.
- **Stops one edit** (through `pre_tool_call`) when it would recreate a failed version. A second try goes through.
- **Reads your message** (through `pre_llm_call`) only to spot "still broken". It keeps fingerprints and verdicts in memory, per session. Nothing is written to disk.
- **No model calls, no network, no dependencies.**

## Limits

- **One session at a time.** Versions from yesterday's session aren't remembered.
- **Only `write_file` and `patch` edits are tracked.** Files changed by shell commands aren't.
- **Exact versions only.** A version that differs by one character from a failed one is a new version, as it should be.

## Development

```bash
pytest tests
```

17 tests cover writes and patches, whitespace, passing versions, versions that later passed, the user saying "still broken", commands that aren't checks, per-file verdicts, sessions, the modes, path resolution and the status command.

## License

MIT
