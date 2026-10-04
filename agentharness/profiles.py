"""Conservative local CPU presets; no automatic downloads or cloud fallback."""
PROFILES = {
    'default': dict(model='qwen2.5-coder:3b', max_tokens=4096, text_tools=False,
                    max_context_chars=80000, tool_output_chars=6000),
    'laptop-i3-12gb': dict(model='nessa-i3:latest', max_tokens=1024, text_tools=True,
                          max_context_chars=18000, tool_output_chars=2500),
}


def apply_profile(args):
    profile = PROFILES[args.profile]
    for key, value in profile.items():
        if getattr(args, key, None) is None:
            setattr(args, key, value)
    return args
