"""Conservative local CPU presets; no automatic downloads or cloud fallback."""
PROFILES = {
    # LFM emits reliable native <|tool_call_start|> calls; in JSON-text mode it often wrote
    # bare arguments without a tool name, so project work never started (2026-10-04 probe).
    'lfm-i3-12gb': dict(model='nessa-lfm:latest', max_tokens=3072, text_tools=False,
                       max_context_chars=18000, tool_output_chars=2500, temperature=0.2,
                       reasoning_effort='none'),
    # Same LFM2.5 weights with a 32K-token window (model supports 128K). Its hybrid conv/attention
    # layers keep the KV cache small enough for 12 GB RAM. Build: configs/Modelfile.lfm-32k.
    'lfm-32k': dict(model='nessa-lfm-32k:latest', max_tokens=3072, text_tools=False,
                    max_context_chars=96000, tool_output_chars=8000, temperature=0.2,
                    reasoning_effort='none', compact_at_tokens=26000),
    'default': dict(model='qwen2.5-coder:3b', max_tokens=4096, text_tools=False,
                    max_context_chars=80000, tool_output_chars=6000),
    'laptop-i3-12gb': dict(model='nessa-i3:latest', max_tokens=1024, text_tools=True,
                          max_context_chars=18000, tool_output_chars=2500),
}


def apply_profile(args):
    profile = PROFILES[args.profile]
    for key, value in profile.items():
        if key == 'compact_at_tokens':
            continue  # read by the CLI from PROFILES directly
        if getattr(args, key, None) is None:
            setattr(args, key, value)
    return args
