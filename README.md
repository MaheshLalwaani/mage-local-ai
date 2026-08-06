# Magento Customization Advisor

An offline-first assistant for exploring Magento 2 customizations. It indexes an
explicitly selected Magento checkout, retrieves relevant local evidence, and asks
a locally running Ollama model to assess a requirement. It does not send source
code or prompts to a cloud service.

The tool is read-only by default. It can propose a patch for review, but it never
applies a change to Magento.

## What it can do

- Assess a requirement as `USE_CORE`, `EXTEND_EXISTING`,
  `NEW_CUSTOMIZATION`, or `INSUFFICIENT_EVIDENCE`.
- Explain the structure and tests of a custom `Vendor_Module`.
- Review an allowlisted PHP file for evidence-backed Magento code issues.
- Produce a PHPUnit 9.5 test plan without creating files.
- Optionally propose an evidence-based unified diff with `assess --include-code`.
- Run a small, approval-gated set of read-only Magento commands.

## Requirements

- Python 3.11 or later.
- [Ollama](https://ollama.com/download), running on the same machine at
  `http://127.0.0.1:11434`.
- A Magento 2 checkout containing `bin/magento`.
- `vendor/magento` only if you want to use `index-core`.

This project has no Python runtime dependencies beyond the standard library.

## Install

```bash
git clone https://github.com/<your-github-username>/magento-local-agent.git
cd magento-local-agent

python3 -m venv .venv
.venv/bin/pip install -e .

# In a separate terminal if Ollama is not already running as a service:
ollama serve
```

Pull a model after choosing one from the hardware guide below. The default is
`qwen2.5-coder:7b`:

```bash
ollama pull qwen2.5-coder:7b
```

Confirm that the tool is available:

```bash
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli --help
ollama list
```

The examples below use `PYTHONPATH=src .venv/bin/python -m
magento_local_agent.cli` rather than the optional `magento-agent` console
script. Run them from this repository root; this form works even when the
console-script entry point is not on your shell `PATH`.

## First use

Replace `/path/to/magento` with the absolute path to your Magento checkout.

```bash
# Index only allowlisted project files.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli index /path/to/magento

# Optional: index Magento core declarations. This enables USE_CORE decisions.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli index-core /path/to/magento

# Assess a requirement using the default model.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli assess /path/to/magento \
  "Add a customer field during checkout"

# Request a reviewable proposal. This never writes or applies the patch.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli assess /path/to/magento \
  "Add a customer field during checkout" \
  --include-code
```

Answers stream token by token. Use `--no-stream` to print the completed answer
at the end instead. Re-run `index` and, if used, `index-core` after relevant
Magento code changes.

## Choosing a local model

The advisor sends an 8K-token context to Ollama. Model size, context length,
other running applications, and whether the model fits in GPU VRAM all affect
memory use and speed. The RAM/VRAM values below are practical starting points,
not guarantees; leave several GB free for the OS and Magento/Docker.

| Hardware available | Suggested model | Download | Notes |
| --- | --- | --- | --- |
| 8 GB RAM, CPU only | `qwen2.5-coder:1.5b` | `ollama pull qwen2.5-coder:1.5b` | Small and slow on CPU; useful for smoke tests. |
| 12 GB RAM or 6 GB VRAM | `qwen2.5-coder:3b` | `ollama pull qwen2.5-coder:3b` | Minimum practical option for short assessments. |
| 16 GB RAM or 8 GB VRAM | `qwen2.5-coder:7b` | `ollama pull qwen2.5-coder:7b` | Default and recommended balance for most developer machines. |
| 24–32 GB RAM or 12–16 GB VRAM | `qwen2.5-coder:14b` | `ollama pull qwen2.5-coder:14b` | Better reasoning and patch proposals. |
| 48 GB+ RAM or 24 GB+ VRAM | `qwen2.5-coder:32b` | `ollama pull qwen2.5-coder:32b` | Highest-quality Qwen 2.5 Coder option; much slower if it spills to CPU. |
| 32 GB+ RAM or 24 GB+ VRAM | `qwen3-coder:30b` | `ollama pull qwen3-coder:30b` | Stronger coding model, with a roughly 19 GB Ollama download. |

Use a downloaded model with `--model`:

```bash
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli --model qwen2.5-coder:14b assess \
  /path/to/magento \
  "Add an admin configuration field for the import limit" \
  --include-code
```

### Slow-machine and timeout fallback

If the default `qwen2.5-coder:7b` is slow to start, times out, or makes the
machine unresponsive, use a smaller model. Keep streaming enabled (the default)
so Ollama can return tokens while it works; do **not** add `--no-stream` for a
long test-generation request.

```bash
# First choice for lower-memory or CPU-only machines.
ollama pull qwen2.5-coder:3b
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli --model qwen2.5-coder:3b assess \
  /path/to/magento "Explain the product import validation flow"

# Fastest fallback; expect lower-quality Magento reasoning and tests.
ollama pull qwen2.5-coder:1.5b
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli --model qwen2.5-coder:1.5b assess \
  /path/to/magento "Explain the product import validation flow"
```

The Qwen 2.5 Coder family provides 0.5B, 1.5B, 3B, 7B, 14B, and 32B variants;
its Ollama page lists the current download sizes and tags. Qwen3-Coder offers a
30B local variant as well. Check the [Qwen2.5-Coder library page](https://ollama.com/library/qwen2.5-coder)
and [Qwen3-Coder library page](https://ollama.com/library/qwen3-coder) before
choosing a model, as available tags and sizes can change.

## Workflows

```bash
# Ask a question using indexed project code.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli ask /path/to/magento \
  "Where is the product import validation implemented?"

# Review one PHP source file. It reports findings only and makes no changes.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli review /path/to/magento \
  app/code/Vendor/Module/Model/ImportValidator.php

# Add an evidence-based proposed diff for the reviewed file; it is never applied.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli review /path/to/magento \
  app/code/Vendor/Module/Model/ImportValidator.php \
  --include-code

# Inventory a custom module without calling a model.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli explain /path/to/magento Vendor_Module

# Ask for a test plan; this does not create test files.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli tests /path/to/magento Vendor_Module

# Generate and write one PHPUnit 9.5 test file from the selected class source.
# This creates or replaces only the calculated Test/Unit/...Test.php path.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli tests /path/to/magento Vendor_Module \
  --target Model/Foo.php \
  --include-code --write --approve RUN

# Display a proposed read-only command. Add --approve RUN to execute it.
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli command /path/to/magento module-status
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli command /path/to/magento php-lint app/code/Vendor/Module/Model/Foo.php
```

The command allowlist is `module-status`, `cache-status`, and `php-lint
<relative.php file>`. The exact command is shown first and requires
`--approve RUN` to execute. Docker Compose projects can use `--runner docker
--service phpfpm`.

### Code review

`review <relative.php file>` reads one allowlisted PHP source file directly and
sends its line-numbered contents to the local model. It returns only findings
that the supplied file supports, covering correctness, Magento conventions,
dependency injection, error handling, performance, security, and testability.
Every finding should include severity, exact line numbers, evidence, risk, and
a recommendation. Pass `--include-code` to request a unified diff for that
file only; the diff is printed for review and is never applied.

### Test generation

`tests` without flags creates a test plan from the module inventory. Add
`--include-code --target <path>` to generate a complete proposed PHPUnit 9.5
unit-test file for one custom-module PHP class. The target path is relative to
the module, for example `Model/ImportValidator.php`; it must not point inside
`Test/`. The source is supplied to the local model as evidence, so this mode is
more useful than a file-name-only test plan.

Add `--write --approve RUN` to create the calculated `Test/Unit/...Test.php`
file, or replace it if it already exists. The tool accepts only one complete
fenced PHP test class and refuses ambiguous or malformed output; it never
writes another path. While writing, it shows a progress message and dots rather
than printing the generated PHP. Generated test classes must include a PHPDoc
class docblock and a meaningful PHPDoc block on every `test...` method,
including an `@covers` target and `@return void`. PHPDoc is a generation
guideline rather than a write-blocking requirement, so review the resulting
diff before committing. The tool does not run PHPUnit or claim coverage; those
will be separate, approval-gated steps.

For example, this command generates a test with the smaller 3B model and
creates or replaces `app/code/Vendor/Module/Test/Unit/Model/FooTest.php`:

```bash
PYTHONPATH=src .venv/bin/python -m magento_local_agent.cli --model qwen2.5-coder:3b tests /path/to/magento Vendor_Module \
  --target Model/Foo.php \
  --include-code --write --approve RUN
```

While writing, the terminal displays a progress message and dots rather than
the generated PHP. It reports the exact path after the file is written.

## Security and data handling

- Ollama is contacted only at `127.0.0.1`; this tool has no cloud endpoint or
  telemetry.
- `index` reads only `app/code`, `app/design`, `app/etc/config.php`, `dev/tests`,
  and `composer.json`. It rejects `env.php`, `.env`, keys, database dumps,
  media, logs, `vendor`, and generated files before reading them.
- `index-core` is an explicit, separate exception. It reads only selected Magento
  declaration files and API interfaces under `vendor/magento`, then stores
  declarations such as events, plugins, routes, tables, and interface methods—
  never PHP method bodies.
- SQLite index and audit data remain under `data/`, which is gitignored. Prompts
  are excluded from audit records unless `--audit-prompts` is supplied.
- Proposed patches are output only; the tool does not apply them.

Review these controls with your security team before indexing production code.

## Retrieval and limitations

Search uses local SQLite FTS5: query terms are stopword-filtered, combined with
`OR`, ranked with BM25 and path weighting, and capped at two chunks per file.
`assess` prioritizes Magento declarations in `etc/`; `ask` uses a more balanced
profile for implementations and design notes.

- Retrieval is lexical, so requirements expressed only in business language can
  miss code that uses unrelated terminology.
- Core indexing covers `vendor/magento` only, not third-party extensions.
- Core XML uses Python's `xml.etree`, which is not hardened against entity
  expansion. Only index a trusted vendor tree you already execute.
- AI output, especially a proposed patch, requires normal code review and tests.

## Contributing and publishing

Before making a public GitHub repository, add a `LICENSE` file and choose a
license suitable for your goals (for example, MIT or Apache-2.0). Do not commit
the generated `data/` directory, virtual environments, `.env` files, Magento
checkouts, or proprietary source code.

Bug reports and pull requests should include the command used, the model name,
and a redacted error/output sample. Please do not include secrets or customer
data in an issue.
