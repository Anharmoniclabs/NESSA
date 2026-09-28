# CELL 1: start (or reuse) the Gemma 4 vLLM server on 4x L4, configured like Kaggle scoring.
# Paths are pinned to the three attached inputs. Nothing here runs or scores agent tasks.
from pathlib import Path
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ALLOW_DOWNLOADS = True  # False = strictly offline. True permits PyPI if local wheels are incomplete.
MODEL_NAME = "gemma-4-31b-it-qat-w4a16-ct"
PORT = 8000
ENV = Path("/kaggle/working/vllm_l4")
OUT = Path("/kaggle/working/l4_setup_logs")
LOG = OUT / "server.log"

# Your attached inputs.
MODEL_DIR = Path("/kaggle/input/models/google/gemma-4/other/gemma-4-31b-it-qat-w4a16-ct/2")
WHEELHOUSE_DIR = Path("/kaggle/input/datasets/metric/gemma-4-developer-agent-wheelhouse")
COMPETITION_DIR = Path("/kaggle/input/competitions/gemma-4-developer-agent")

# Match the scoring server (HARNESS_README section 3.1): 0.80 GPU memory, 32k context,
# gemma4 tool/reasoning parsers, thinking enabled by default.
GPU_MEMORY_UTILIZATION = "0.80"
MAX_MODEL_LEN = "32768"


def find_model_dir(root):
    # The model folder may hold config.json directly or one level down.
    hits = sorted({p.parent for p in root.rglob("config.json") if (p.parent / "chat_template.jinja").is_file()})
    if len(hits) != 1:
        raise RuntimeError(f"Expected one model folder with config.json + chat_template.jinja under {root}; found {hits}")
    return hits[0]


def discover_inputs():
    for label, p in (("model", MODEL_DIR), ("wheelhouse", WHEELHOUSE_DIR), ("competition", COMPETITION_DIR)):
        if not p.is_dir():
            raise RuntimeError(f"{label} input not found at {p}. Attach it with 'Add Input'.")
    comp = COMPETITION_DIR
    if not (comp / "tasks.jsonl").is_file() or not (comp / "snapshots").is_dir():
        raise RuntimeError(f"{comp} is missing tasks.jsonl or snapshots/.")
    model = find_model_dir(MODEL_DIR)
    if not any(model.glob("*.safetensors")):
        raise RuntimeError("No model weight files found. Attach the checkpoint, not just its config.")
    # Only the wheelhouse feeds the vLLM install. The competition's own wheels/ folder holds
    # old repository dependencies (pydantic, requests, ...) and must NOT be mixed in.
    wheels = sorted(WHEELHOUSE_DIR.rglob("*.whl"))
    grader = [p for p in wheels if p.name.startswith("swegemma-")]
    if not grader:
        raise RuntimeError(f"No swegemma wheel under {WHEELHOUSE_DIR}.")
    links = sorted({p.parent.resolve() for p in wheels})
    return {"wh": grader[0].parent, "comp": comp, "model_dir": model, "wheels": wheels, "links": links}


def child_env():
    # Drop Python/pip overrides. The existing notebook environment is not modified.
    keep = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ",
            "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES",
            "NVIDIA_DRIVER_CAPABILITIES", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", PYTHONNOUSERSITE="1")
    return env


def stop_owned(proc):
    # Only the process group this cell just created. Never use an old PID file.
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        except ProcessLookupError:
            pass


def logged(argv, name, *, timeout=1800, check=True):
    path = OUT / name
    started = time.monotonic()
    with path.open("w") as stream:
        proc = subprocess.Popen(list(map(str, argv)), stdout=stream, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=child_env(), start_new_session=True)
        try:
            while proc.poll() is None:
                left = timeout - (time.monotonic() - started)
                if left <= 0:
                    raise TimeoutError(f"Step timed out. See {path}")
                try:
                    proc.wait(timeout=min(30, left))
                except subprocess.TimeoutExpired:
                    print(f"  working... {time.monotonic()-started:.0f}s | {name}", flush=True)
            rc = proc.returncode
        finally:
            stop_owned(proc)
    text = path.read_text(errors="replace")
    if check and rc:
        raise RuntimeError(f"{name} failed:\n{text[-5000:]}")
    return rc, text


def request_json(route, body=None, timeout=10):
    # Local model traffic must not pass through a configured HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{route}",
          data=None if body is None else json.dumps(body).encode(),
          headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Model HTTP {exc.code}: {exc.read(4000).decode(errors='replace')}") from exc


def healthy():
    try:
        return next((m for m in request_json("/v1/models", timeout=3).get("data", [])
                     if m.get("id") == MODEL_NAME), None)
    except (OSError, ValueError, RuntimeError):
        return None


def choose_vllm(wheels, tags):
    from pip._vendor.packaging.utils import parse_wheel_filename
    candidates = []
    for path in wheels:
        if not path.name.startswith("vllm-0.19.1-"):
            continue
        _, version, _, wheel_tags = parse_wheel_filename(path.name)
        if str(version) == "0.19.1" and wheel_tags & tags:
            candidates.append(path)
    if not candidates:
        raise RuntimeError("No vLLM 0.19.1 wheel compatible with this Python/platform in the wheelhouse.")

    def digest(path):
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()
    if len({digest(p) for p in candidates}) != 1:
        raise RuntimeError("Conflicting compatible vLLM wheels found; keep one wheelhouse version.")
    return candidates[0]


def pip_args(links, target, online, dry_run=False):
    args = [sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check",
            "--python", str(ENV), "install", "--no-cache-dir", "--no-compile"]
    args += ["--index-url", "https://pypi.org/simple", "--retries", "1", "--timeout", "20"] if online else ["--no-index"]
    for directory in links:
        args += ["--find-links", str(directory)]
    if dry_run:
        args += ["--dry-run"]
    return args + [str(target)]  # dependency resolution stays ON


def start_l4():
    OUT.mkdir(parents=True, exist_ok=True)
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("This build targets Kaggle Python 3.12.")
    hw = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                        capture_output=True, text=True, check=True, timeout=20)
    gpus = [s.strip() for s in hw.stdout.splitlines() if s.strip()]
    print("GPUs:", gpus)
    if len(gpus) != 4 or not all("L4" in s for s in gpus):
        raise RuntimeError("This cell targets four visible L4 GPUs. Nothing launched.")
    found = discover_inputs()
    print("Model:", found["model_dir"])
    print("Competition:", found["comp"])
    print("Wheelhouse:", WHEELHOUSE_DIR, f"({len(found['wheels'])} wheels)")
    print("Package downloads allowed:", ALLOW_DOWNLOADS)
    (OUT / "inputs.json").write_text(json.dumps({
        k: [str(p) for p in v] if isinstance(v, list) else str(v) for k, v in found.items()}, indent=2))

    with socket.socket() as sock:
        sock.settimeout(3)
        busy = sock.connect_ex(("127.0.0.1", PORT)) == 0
    info = healthy()
    if busy and not info:
        raise RuntimeError(f"Port {PORT} is occupied but not serving this model. Nothing killed.")
    if info:
        print("Matching server reused; its launch settings are NOT re-verified here.")
        launched = False
    else:
        from pip._vendor.packaging.tags import sys_tags
        wheel = choose_vllm(found["wheels"], set(sys_tags()))
        if not (ENV / "pyvenv.cfg").is_file():
            logged([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", ENV],
                   "create_env.log", timeout=90)
        py = ENV / "bin/python"
        logged([py, "-B", "-c",
                "import sys; from pathlib import Path; assert Path(sys.prefix).resolve()==Path(sys.argv[1]).resolve(); assert sys.prefix!=sys.base_prefix", ENV],
               "check_destination.log", timeout=30)
        check_code = '''import importlib.metadata as md, json, sys
import torch, vllm, transformers
from transformers import AutoConfig
assert md.version("vllm") == "0.19.1"
assert torch.cuda.is_available() and torch.cuda.device_count() == 4
assert all("L4" in torch.cuda.get_device_name(i) for i in range(4))
AutoConfig.from_pretrained(sys.argv[1], local_files_only=True, trust_remote_code=False)
print("STACK="+json.dumps({k:md.version(k) for k in ("vllm","transformers","tokenizers","torch")}))
'''
        rc, text = logged([py, "-B", "-c", check_code, found["model_dir"]],
                          "existing_stack.log", timeout=180, check=False)
        if rc:
            print("Checking whether the wheelhouse is enough for an offline install...")
            offline_rc, _ = logged(pip_args(found["links"], wheel, False, True),
                                   "offline_resolve.log", timeout=300, check=False)
            online = offline_rc != 0
            if online and not ALLOW_DOWNLOADS:
                raise RuntimeError("Wheelhouse is incomplete for this environment. Details: "
                                   + str(OUT / "offline_resolve.log"))
            print("Installing into vllm_l4 only. " +
                  ("Using the wheelhouse; no network." if not online else
                   "Wheelhouse-only resolution failed; trying PyPI. Kaggle Internet must be ON."))
            logged(pip_args(found["links"], wheel, online), "install.log", timeout=2400)
            _, text = logged([py, "-B", "-c", check_code, found["model_dir"]],
                             "ready_stack.log", timeout=180)
        print(next((s[6:] for s in text.splitlines() if s.startswith("STACK=")), "Stack imports passed."))
        entry = ("from importlib.metadata import distribution; "
                 "e=next(e for e in distribution('vllm').entry_points "
                 "if e.group=='console_scripts' and e.name=='vllm'); e.load()()")
        cli = [str(py), "-B", "-c", entry]
        _, help_text = logged(cli + ["serve", "--help=all"], "cli_help.log", timeout=120, check=False)
        if "--default-chat-template-kwargs" not in help_text:  # older CLIs lack --help=all
            _, help_text = logged(cli + ["serve", "--help"], "cli_help.log", timeout=120)
        args = cli + ["serve", str(found["model_dir"]),
            "--host", "127.0.0.1", "--port", str(PORT), "--served-model-name", MODEL_NAME,
            "--tensor-parallel-size", "4", "--gpu-memory-utilization", GPU_MEMORY_UTILIZATION,
            "--max-model-len", MAX_MODEL_LEN, "--enable-prefix-caching",
            "--enable-auto-tool-choice", "--tool-call-parser", "gemma4",
            "--reasoning-parser", "gemma4",
            "--chat-template", str(found["model_dir"] / "chat_template.jinja")]
        if "--default-chat-template-kwargs" in help_text:
            args += ["--default-chat-template-kwargs", json.dumps({"enable_thinking": True})]
        else:
            print("Note: this vLLM has no --default-chat-template-kwargs; thinking default may differ from scoring.")
        (OUT / "launch.json").write_text(json.dumps(args, indent=2))
        print("Starting model: 4-way tensor parallel, 32,768 context, scoring-like settings.")
        started = time.monotonic()
        with LOG.open("w") as stream:
            proc = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
                env={**child_env(), "VLLM_WORKER_MULTIPROC_METHOD": "spawn", "HF_HUB_OFFLINE": "1"})
        try:
            while not healthy():
                if proc.poll() is not None:
                    raise RuntimeError("Server exited:\n" + LOG.read_text(errors="replace")[-6000:])
                if time.monotonic() - started > 1500:
                    raise TimeoutError(f"Model did not become ready within 25 minutes; see {LOG}")
                print(f"  loading... {time.monotonic()-started:.0f}s | {LOG.name}", flush=True)
                time.sleep(20)
        except BaseException:
            stop_owned(proc)
            raise
        info = healthy()
        launched = True
        (OUT / "process.json").write_text(json.dumps({"pid": proc.pid, "launch": args}, indent=2))
        print(f"SERVER READY after {time.monotonic()-started:.0f}s.")

    # One tool-call probe: the agent only works if the server returns structured tool calls.
    probe = request_json("/v1/chat/completions", {
        "model": MODEL_NAME, "temperature": 0, "max_tokens": 512,
        "messages": [{"role": "user", "content": "Call get_status now."}],
        "tools": [{"type": "function", "function": {"name": "get_status", "description": "Budget status.",
                   "parameters": {"type": "object", "properties": {}}}}]}, timeout=600)
    msg = (probe.get("choices") or [{}])[0].get("message", {})
    calls = [c.get("function", {}).get("name") for c in msg.get("tool_calls") or []]
    print("Tool-call probe:", calls or f"NO structured tool call (content={str(msg.get('content'))[:200]!r})")
    (OUT / "probe.json").write_text(json.dumps({"model": info, "new_server_started": launched,
                                                "tool_calls": calls}, indent=2))
    print("\nModel server ready. Logs:", OUT)
    return {**found, "gpus": gpus, "server_info": info}


if __name__ == "__main__":
    _setup = start_l4()
    wh, comp, model_dir, gpus = (_setup[k] for k in ("wh", "comp", "model_dir", "gpus"))
