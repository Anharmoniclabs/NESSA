"""Offline pre-flight check and packager for a Gemma 4 Developer Agent submission.

Mirrors the documented swegemma/adk-submission rules so mistakes surface before a
Kaggle run: one root config, allowed file types, no symlinks or path traversal,
a single allowed model, known tools, generation limits, eval_config keys, adapter
references, and ADK `{placeholder}` templating in instructions.

    python validate_submission.py submission/            # check
    python validate_submission.py submission/ --zip submission.zip
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile
from pathlib import Path

import yaml

ALLOWED_MODEL = "gemma-4-31b-it-qat-w4a16-ct"
ROOT_NAMES = ("agent.yaml", "agent.yml", "root_agent.yaml", "root_agent.yml")
ALLOWED_EXT = {".yaml", ".yml", ".md", ".txt", ".py", ".json", ".safetensors"}
KNOWN_TOOLS = {"run_command", "read_file", "edit_file", "write_file", "get_status", "submit_patch",
               "get_code_neighbors", "search_similar_code", "get_code_subgraph",
               "run_skill_script", "load_skill_resource"}
GEN_KEYS = {"temperature", "top_p", "top_k", "max_output_tokens", "presence_penalty",
            "frequency_penalty", "stop_sequences", "response_mime_type", "seed", "thinking_config"}
GEN_FORBIDDEN = {"tools", "system_instruction", "http_options", "safety_settings", "response_schema"}
EVAL_KEYS = {"timeout_seconds", "max_tool_calls", "max_time_minutes", "max_turns"}
STATE_VARS = {"problem_description"}          # always set by the harness
OPTIONAL_STATE_VARS = {"hints"}               # only set when hints exist: must be written {hints?}
MAX_BYTES = 3 * 1024 ** 3
# ADK replaces {identifier} / {identifier?} in instructions from session state.
PLACEHOLDER = re.compile(r"\{+([^{}]*)\}+")


class Include:
    def __init__(self, path):
        self.path = path


def _loader():
    class L(yaml.SafeLoader):
        pass
    L.add_constructor("!include", lambda loader, node: Include(loader.construct_scalar(node)))
    return L


def _inside(root: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load(root: Path, path: Path, errors: list, depth: int = 0, seen=()):
    """Load YAML and resolve !include relative to the including file."""
    if depth > 10 or path in seen:
        errors.append(f"{path.relative_to(root)}: include depth/cycle limit")
        return None
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_loader())

    def resolve(node):
        if isinstance(node, Include):
            rel = node.path
            if os.path.isabs(rel) or ".." in Path(rel).parts and not _inside(root, path.parent / rel):
                errors.append(f"{path.relative_to(root)}: unsafe include {rel!r}")
                return None
            target = (path.parent / rel)
            if not _inside(root, target) or not target.is_file():
                errors.append(f"{path.relative_to(root)}: include not found {rel!r}")
                return None
            if target.suffix in (".md", ".txt"):
                return target.read_text(encoding="utf-8")
            return load(root, target, errors, depth + 1, (*seen, path))
        if isinstance(node, dict):
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node
    return resolve(data)


def check_instruction(where: str, text: str, errors: list):
    for m in PLACEHOLDER.finditer(text or ""):
        inner = m.group(1).strip()
        name = inner.rstrip("?")
        if not name.isidentifier():
            continue  # ADK leaves non-identifier braces alone
        if name in STATE_VARS:
            continue
        if name in OPTIONAL_STATE_VARS and inner.endswith("?"):
            continue
        errors.append(f"{where}: instruction placeholder {m.group(0)!r} is filled from session state "
                      f"and will fail at runtime (use problem_description or hints? only)")


def check_agent(root: Path, cfg_path: Path, errors: list, models: set, visited: set):
    if cfg_path in visited:
        return
    visited.add(cfg_path)
    where = str(cfg_path.relative_to(root))
    cfg = load(root, cfg_path, errors)
    if not isinstance(cfg, dict):
        errors.append(f"{where}: agent config must be a mapping")
        return
    cls = cfg.get("agent_class", "LlmAgent")
    if not cfg.get("name"):
        errors.append(f"{where}: missing name")
    children = [s.get("config_path") for s in cfg.get("sub_agents") or [] if isinstance(s, dict)]
    if cls == "LlmAgent":
        model = str(cfg.get("model", "")).split("/")[-1]
        models.add(model)
        if model != ALLOWED_MODEL:
            errors.append(f"{where}: model must be {ALLOWED_MODEL}, got {cfg.get('model')!r}")
        check_instruction(where, cfg.get("instruction", ""), errors)
        if cfg.get("adapter"):
            a = root / "adapters" / cfg["adapter"]
            if not (a / "adapter_config.json").is_file() or not (a / "adapter_model.safetensors").is_file():
                errors.append(f"{where}: adapter {cfg['adapter']!r} missing adapter_config.json/adapter_model.safetensors")
        for t in cfg.get("tools") or []:
            if isinstance(t, str):
                if t not in KNOWN_TOOLS:
                    errors.append(f"{where}: unknown tool {t!r}")
            elif isinstance(t, dict) and "agent_tool" in t:
                children.append((t["agent_tool"] or {}).get("config_path"))
            else:
                errors.append(f"{where}: unsupported tool entry {t!r}")
        gen = cfg.get("generate_content_config") or {}
        for k in gen:
            if k in GEN_FORBIDDEN or k not in GEN_KEYS:
                errors.append(f"{where}: generate_content_config.{k} is not allowed")
        mot = gen.get("max_output_tokens")
        if mot is not None and not (1 <= int(mot) <= 32768):
            errors.append(f"{where}: max_output_tokens must be 1..32768")
        tb = (gen.get("thinking_config") or {}).get("thinking_budget")
        if tb is not None and not (0 <= int(tb) <= 32768):
            errors.append(f"{where}: thinking_budget must be 0..32768")
        if mot and tb and int(tb) >= int(mot):
            errors.append(f"{where}: thinking_budget should be below max_output_tokens")
    elif cls == "LoopAgent" and not (1 <= int(cfg.get("max_iterations", 500)) <= 500):
        errors.append(f"{where}: max_iterations must be 1..500")
    elif cls not in ("SequentialAgent", "ParallelAgent", "LoopAgent"):
        errors.append(f"{where}: unknown agent_class {cls!r}")
    for rel in children:
        if not rel:
            errors.append(f"{where}: sub-agent entry without config_path")
            continue
        child = cfg_path.parent / rel
        if not _inside(root, child) or not child.is_file():
            errors.append(f"{where}: sub-agent config not found {rel!r}")
            continue
        check_agent(root, child.resolve(), errors, models, visited)


def validate(root: str | Path) -> list[str]:
    root = Path(root).resolve()
    errors: list[str] = []
    roots = [root / n for n in ROOT_NAMES if (root / n).is_file()]
    if len(roots) != 1:
        return [f"expected exactly one root config {ROOT_NAMES}, found {len(roots)}"]
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                errors.append(f"symlink not allowed: {p.relative_to(root)}")
        for name in filenames:
            p = Path(dirpath) / name
            total += p.stat().st_size
            if p.suffix.lower() not in ALLOWED_EXT and name != ".gitkeep":
                errors.append(f"file type not allowed: {p.relative_to(root)}")
    if total >= MAX_BYTES:
        errors.append(f"submission is {total} bytes; limit is < 3 GiB")
    models: set = set()
    check_agent(root, roots[0], errors, models, set())
    if len(models) > 1:
        errors.append(f"all agents must share one model, found {sorted(models)}")
    ev = root / "eval_config.yaml"
    if ev.is_file():
        data = yaml.safe_load(ev.read_text()) or {}
        section = data.get("evaluation")
        if not isinstance(section, dict):
            errors.append("eval_config.yaml: settings must be under an `evaluation:` key")
        else:
            for k, v in section.items():
                if k not in EVAL_KEYS:
                    errors.append(f"eval_config.yaml: unknown key evaluation.{k}")
                elif not isinstance(v, (int, float)) or v <= 0:
                    errors.append(f"eval_config.yaml: evaluation.{k} must be a positive number")
    return errors


def pack(root: str | Path, out: str | Path) -> Path:
    """Zip with agent.yaml at the archive root (no wrapping folder)."""
    root, out = Path(root).resolve(), Path(out).resolve()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.name != ".gitkeep" and p != out:
                z.write(p, p.relative_to(root).as_posix())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("submission_dir")
    ap.add_argument("--zip", help="write submission.zip here when validation passes")
    a = ap.parse_args(argv)
    errors = validate(a.submission_dir)
    for e in errors:
        print("ERROR:", e)
    if errors:
        return 1
    print("Submission OK.")
    if a.zip:
        print("Packed:", pack(a.submission_dir, a.zip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
