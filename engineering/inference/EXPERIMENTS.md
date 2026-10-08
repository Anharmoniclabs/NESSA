# Inference experiments — 2026-10-08

## Implemented

- Cloud clients stream even without a UI callback. Replies and agent model events
  carry requested/served model, completion/cache/reasoning tokens, first generated
  event, first visible text, generation span and request duration. Missing usage
  remains null. Successful fallback replies include attempted models and timings.
- Prompt events break down schema bytes and message bytes by role. Compaction
  events record serialized bytes before/after. Existing digest and atomic tool
  exchanges remain intact.
- Batch CLI constructs a separate client/fallback chain for every task. Legacy
  callers without a factory execute serially to avoid shared mutable routing state.
- `benchmarks/inference.py` provides bounded cloud and native Ollama probes.

TTFT is client-observed time to a nonempty generation event, including reasoning
or native tool fragments. It includes routing, network, queuing and prefill; it
is not a measurement of pure cloud prefill. Role-only events do not count.
Cloud decode rate is an estimate `(completion_tokens-1)/generation_span` because
SSE chunks may contain multiple tokens and buffered reasoning. Local decode rate
uses Ollama's server-reported evaluation count/duration. Counts are not bills.

## Evidence

Artifacts: `/home/alabs/nessa-inference-live-2026-10-08/`. Earlier directory
`nessa-inference-2026-10-08` contains sandbox connection failures, not measurements.
Cloud route pinned to `zai-org/GLM-5.3:novita`, temperature 0, max output 128,
streaming, no retries; direct diagnostic requests deliberately exclude fallback.
Production still has local fallback. 16 cloud requests all completed at the output
limit; these measure serving, not task correctness. Requests ran in fixed order;
provider load, cache residency and reasoning lengths are uncontrolled.

| Probe | Observed result | Interpretation |
|---|---|---|
| Repeated 2,235-token prefix | 2,176 cached tokens in all three subsequent identical requests | Prefix reuse works on this route |
| Change beginning of prefix | 0 cached tokens on both changes | Stable beginnings matter |
| First long prompt vs first repeat | TTFT 1.202 vs 1.200 s | No convincing latency benefit at this size |
| Four sequential cloud requests | 15.892 s total | One batch only |
| Four requests, two workers | 6.425 s total | 2.47x observed batch throughput; not a guarantee |
| Local first/repeated prompt | Prefill 1.243 / 0.240 s; cached 0 / 21 of 25 tokens | Existing cache effective in short probe |
| Local first/repeated decode | 10.86 / 11.18 tokens/s | Cache mainly helped prefill |
| Synthetic compaction | 36,383 -> 3,811 bytes, 0.61 ms | Preserved last three complete tool exchanges; not a quality trial |

Local runtime: Ollama 0.35.1, `nessa-lfm-32k:latest`, Q4_K_M 8.5B LFM2 MoE,
32,768 loaded context, 5.73 GB reported resident model size, zero VRAM use.
Only two local generations, 32 output tokens each. Short-prompt results do not
predict long-context behavior.

## Hypotheses and next gates

| Mechanism | Hypothesis / test | Current decision |
|---|---|---|
| Prefill/TTFT | Stable system/tools prefix lowers repeated input work; alternate matched long prompts across at least 20 pairs | Preserve prefix order; measure hits; no latency claim yet |
| Decode | Long outputs isolate generation speed; compare same reasoning/output budget | Instrumented; no reasoning-budget default change |
| KV cache | Quantized cache may lower memory at long context; compare f16/q8 with 4k/16k/32k contexts and independent recall/coding checks | Cache reuse measured; KV dtype tuning untested |
| Compaction | Smaller history reduces input cost but can erase needed evidence and invalidate prefix reuse | Existing atomic compaction retained, measured; quality gate required before lowering limits |
| Speculative decoding | Draft/verify may lower inter-token latency in memory-bound serving; sweep speculation lengths and measure acceptance, latency, memory and quality | Requires controllable compatible server; untested on current backend |
| Disaggregation | Separate prefill/decode workers may improve tail latency under mixed load | Requires separate serving instances and KV transport; unavailable through current HF router configuration |
| Parallelism | Independent requests overlap; shared mutable clients corrupt routing state | Isolation fix implemented; test two workers; dependent tool effects stay ordered |
| GPU tensor/pipeline parallelism | Splitting weights/layers can fit larger models but adds communication | No multi-GPU server under our control; not implemented |

Do not add an answer cache for code/tool results without repository-state and
permission-aware invalidation. Prefix KV reuse is distinct from reusing an answer.
Do not infer server speculation or KV dtype from observed tokens/second.

## Backend references

[Ollama usage](https://docs.ollama.com/api/usage) defines nanosecond timing,
uncached prefill and cache counts. [Ollama FAQ](https://docs.ollama.com/faq)
documents parallelism memory costs and global KV formats requiring Flash Attention.
[vLLM speculation](https://docs.vllm.ai/en/latest/features/speculative_decoding/)
requires compatible serving configuration and is workload dependent.
[vLLM disaggregation](https://docs.vllm.ai/en/latest/features/disagg_prefill/)
separates prefill/decode for latency control and explicitly does not promise
throughput improvement. These are backend capabilities, not client flags.

## Validation

- 92 controller, streaming, fallback, compaction and batch tests passed with the
  local fake HTTP server enabled. Follow-up cloud/desktop tests: 35 passed;
  focused accounting/streaming/benchmark tests: 26 passed. These overlap.
- Live production cloud factory: native streamed `report(value=42)` assembled
  correctly; first generated event 1.206 s. This tests transport, not code quality.
- Three additional nonstreaming coding runs: pagination and dependencies passed
  independent checks; invoice hit a 60-second cloud read timeout followed by a
  local fallback timeout. Artifacts at
  `/home/alabs/nessa-stream-validation-2026-10-08/`; despite its directory name,
  this run used the original nonstreaming benchmark transport. It is not a
  streaming validation or a clean performance comparison with the earlier pilot.
- Full streaming agent loop: pagination passed public and independent hidden
  checks, no fallback, 55.60 s. Artifacts:
  `/home/alabs/nessa-stream-tool-harness-2026-10-08/`. One task validates integration;
  it does not establish a coding speedup.
