# Opt-in code-only solve transport

This is an unpromoted experiment. The default three-stage tool flow is unchanged.
It was motivated by a diagnostic where the same 1.5B model repaired a boolean
parser in a direct code-only request but failed inside the tool-oriented solve
wrapper. Another direct-code diagnostic still failed, so this is not evidence
that simpler output alone solves the general problem.

```bash
python -m agentharness.three_stage PROJECT 'Repair the described behavior' --solve-mode code-only
```

Intake still proposes a plan and requires approval (or explicit `--auto-approve`).
The optional solve transport supports exactly one approved, existing, unprotected
Python file with valid original syntax and statically supported interfaces.
Syntax-broken originals, duplicate qualified declarations and wildcard imports
are unsupported because the interface guard cannot be established. Use tools
mode for those tasks. Unsupported scope fails before source generation; it never silently
switches modes or edits another file.

Solve receives the original task, exact current source, applicable project
instructions and, after a failed attempt, controller feedback. It receives no
tool menu or algorithmic plan steps. Its output is a source-content proposal,
not an instruction to execute tools. The controller fixes the destination and
exact preimage from the approved workspace; model-supplied paths are not used.

Admission checks:

- Source input and accepted replacement source are capped at 12,000 characters;
  the raw response (including any outer fence) is capped at 24,000. The source cap
  is lowered when needed to retain room for bounded correction feedback
- Empty, truncated/native-tool responses, JSON/tool descriptions, prose wrappers,
  invalid Python and multiple Markdown blocks are rejected
- Raw Python or one outer Python code fence is accepted; only that outer transport
  fence is removed. Edit content is never guessed, auto-corrected or normalized
- Existing function/class declarations, arguments, annotations, decorators and
  syntactic module bindings must remain, including declarations within module
  control flow. Function/class-local and comprehension-target variables are not
  treated as module bindings. Added helpers are allowed
- The existing approved-path, protected-test, freshness, no-op and atomic-write
  gates still apply
- Only the existing independent verification flow executes candidate project
  code, in disposable copies with integrity checks. AST parsing/compilation does
  not execute generated code

There are at most `min(max_steps, max_proposals)` full-source attempts. A rejected
proposal can be corrected with concrete controller feedback. A syntax-valid but
wrong edit must still pass every required check before `verified` is possible.
A no-change or failed result returns a nonzero CLI exit code. Respond remains a
separate bounded model call whose prose is advisory; controller facts remain
independently authoritative.

This transport preserves declared interfaces, not semantics. It cannot prove
that unrelated behavior is unchanged, and it is not an OS sandbox. Use trusted
projects/checks or a separately isolated execution environment. Live comparisons
must keep this opt-in mode separate from model-size and source-grounding changes,
retain failed cases and label previously inspected failures as development replays.

Offline validation:

```bash
python -m unittest agentharness.tests.test_code_proposals -q
python -m unittest discover -q
```
