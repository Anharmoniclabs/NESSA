# Inference engineering working context

Read README.md, SPEC.md and FINDINGS.md before changing inference behavior. Update
STATE.md with decisions, actual changes, validation and the next unresolved step.

- Preserve one parent controller, permissions, operation receipts and final verification.
- Keep cloud opt-in and local fallback; mark fallback separately in evaluations.
- Pin task, model, provider, configuration and code revision for comparisons.
- Distinguish API-reported tokens, serialized prompt bytes and actual billed cost.
- Use independent checks; never score the controller's own completion claim as correctness.
- Keep secrets out of prompts, reports and fixtures. Store large run artifacts outside git.
- State what was measured, sample size and limits. Do not tune on the held-out grading data.
- Change one inference mechanism at a time and retain the prior measurements.
- Do not assume the native desktop's enterprise mode is equivalent to the private-copy pilot.

This workspace is repository context and specifications; it does not provision
cloud hardware, launch background agents or change production defaults by itself.
