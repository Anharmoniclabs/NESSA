# agentharness

A small, local-first autonomous coding agent harness, plus a document-to-table
extractor (OCR + regex). Standard library only.

```text
inspect → plan → approve → edit a private copy → verify → patch + evidence
```

**The model proposes; the controller decides** what tools are permitted, whether a
plan is approved, and whether the work counts as done. Your original project is never
modified. You get a patch to review, and `--apply` offers to `git apply` it.

## Quick start (desktop, Ollama)

```bash
ollama pull qwen2.5-coder:3b
python -m agentharness doctor
python -m agentharness run ~/code/myproject "Fix the crash when the config file is empty"
python -m agentharness run ~/code/myproject --task-file task.md --show-diff --apply
```

Another server: `--base-url http://127.0.0.1:8000/v1 --model <name>` (vLLM, llama.cpp),
or set `AGENT_BASE_URL` / `AGENT_MODEL`. Non-local servers are refused unless you pass
`--allow-remote`. If the model has no native tool calling, add `--text-tools`.

## How a run works

| Stage | What happens |
|---|---|
| Snapshot | The project is copied to `~/.agentharness/runs/<name>-<time>/repo` (plus a `baseline/` copy for diffs and undo). |
| Baseline | Tests run once before any change, so the model and the report know what already failed. |
| Plan | Read-only tools only. The model calls `propose_plan`. You approve it, reject it, or type feedback to get a revised plan. |
| Execute | Full tools. An edit is refused unless the model has read the file's current version. Duplicate reads get flagged. After 12 actions with no change the model is nudged, and after 24 the run stops as `stalled`. |
| Finish gate | When the model calls `finish`, the controller re-runs `syntax` and `tests` itself. If they fail, the model is sent back (twice by default) with the failure output. |
| Evidence | `evidence/`: `events.jsonl` (ordered log), `patch.diff`, `result.json`, `messages.json`, `journal.json` |

**Statuses:** `verified` (tests passed) · `unverified` (changed, but no test could confirm
it) · `improved` (fewer failures than the baseline) · `failed_checks` · `no_change` ·
`stalled` · `budget_exhausted` · `rejected` / `no_plan` · `error`. A setup problem, a
timeout or missing tests is never reported as a pass.

**Tools:** `list_dir`, `search`, `outline`, `read_file`, `replace_in_file`, `edit_lines`,
`write_file`, `undo_file`, `show_diff`, `run_check`, `run_command` (disable with
`--no-shell`), `extract_text`, `extract_table`, `propose_plan`, `finish`.

Custom checks: `--check "tests=pytest -q tests/unit" --check "lint=ruff check ."`.

## Document extraction (OCR + regex → table)

```bash
python -m agentharness extract invoices/*.pdf scans/*.png \
  --field "invoice:Invoice No=@id" --field "date:Date=@date" \
  --field "total:Total Due=@money" --field "email=@email" \
  --tables --out invoices.csv --tables-out line_items.csv
python -m agentharness extract invoices/*.pdf --spec invoice_spec.json --group-by vendor --sum total
```

- **Inputs:** text, HTML, DOCX, PDF (`pdftotext`, or `pypdf`), and scanned PDFs or images
  (`tesseract` with `pdftoppm`). Missing tools produce a warning, not empty data.
- **OCR repair:** only touches tokens that are clearly numbers (`1O0.5O` → `100.50`,
  `INV-2O26` → `INV-2026`). It also re-joins hyphenated words and keeps column spacing.
- **Fields:** `name[:Label]=pattern[:type]`. The pattern is a regex (a named group
  `value`, or the first group, is captured) or a built-in pattern: `@money @number @int
  @percent @date @time @email @phone @url @id @zip @ipv4 @word @text`. With a label, the
  value is looked for after the label on the same line (dot leaders are fine), then on
  the next line.
- **Types:** `money`/`float` understand `$1,234.50`, `1.234,50` and `(12.00)`. `date`
  becomes ISO format (add `--dayfirst` for 03/04 = 3 April).
- **Tables:** detected from pipes, tabs, or 2+ spaces between columns, with headers
  inferred. `--kv` collects every `Key: Value` line.
- **Output:** `.csv .tsv .json .jsonl .md`. `--group-by` and `--sum` add totals.

Spec file example:

```json
{"fields": [
  {"name": "invoice", "label": "Invoice No", "pattern": "@id", "required": true},
  {"name": "total", "label": "Total Due", "pattern": "@money"},
  {"name": "amounts", "pattern": "\\$[\\d,]+\\.\\d\\d", "type": "money", "multiple": true}
], "tables": true}
```

## Batch runs

```bash
python -m agentharness batch --comp <folder with tasks.jsonl + snapshots/> --workers 4 --limit 2
```

Runs the harness unattended over a `tasks.jsonl` (plans auto-approved, resumable).
This is for your own experiments. The Gemma 4 Developer Agent competition runs its
own harness (`swegemma`) on a declarative submission; that lives in
`kaggle/gemma4_submission/`.

## Extension points

- **Action policy** (`policy.py`): a ranker such as NEUDO can reorder or narrow the
  permitted tools. In `shadow` mode it is only logged. It can never add a tool,
  approve a plan or mark work done.
- **Lessons** (`memory.py`): `python -m agentharness lesson add <project> "Run tests
  with make test"`. Only notes you add are stored, and the relevant ones go into
  future prompts.

## Tests

`python -m pytest -q agentharness`: 36 tests using a scripted model and a fake local
HTTP server. They exercise the control flow, not model quality. Whether a given model
reliably fixes real code has to be measured on real tasks.
