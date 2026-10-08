"""Bounded inference probes; direct diagnostics never count as fallback success."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import platform
from pathlib import Path
import sys
import time
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentharness.cloud import HF_ROUTER, load_token
from agentharness.llm import ChatClient


def cloud_probe(model, prompt):
    client = ChatClient(HF_ROUTER, model, allow_remote=True, api_key=load_token(),
                        stream=True, max_tokens=128, timeout=90, retries=1)
    start = time.monotonic()
    try:
        reply = client.chat([{'role': 'user', 'content': prompt}])
        return dict(reply.inference, finish_reason=reply.finish_reason, status='ok')
    except Exception as exc:
        # No prompt, response or credentials in diagnostics.
        return dict(status='error', error_type=type(exc).__name__, seconds=time.monotonic()-start)


def local_probe(base, model, prompt):
    body = dict(model=model, prompt=prompt, stream=True, options=dict(num_predict=32, temperature=0))
    req = urllib.request.Request(base+'/api/generate', data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    start = time.monotonic()
    first = None
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            for line in response:
                if time.monotonic()-start > 120:
                    raise TimeoutError('Local probe deadline')
                data = json.loads(line)
                if data.get('error'):
                    raise RuntimeError('Ollama rejected generation')
                if (data.get('response') or data.get('thinking')) and first is None:
                    first = time.monotonic()-start
                if data.get('done'):
                    result = {k: data.get(k) for k in ('model', 'total_duration', 'load_duration',
                        'prompt_eval_count', 'prompt_eval_cached_count', 'prompt_eval_duration',
                        'eval_count', 'eval_duration', 'done_reason')}
                    duration = result['eval_duration']
                    result.update(status='ok', ttft_seconds=first,
                        decode_tokens_per_second=result['eval_count']/(duration/1e9) if duration else None)
                    return result
        raise RuntimeError('Incomplete response')
    except Exception as exc:
        return dict(status='error', error_type=type(exc).__name__, seconds=time.monotonic()-start)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cloud', action='store_true')
    p.add_argument('--model', default='zai-org/GLM-5.3:novita')
    p.add_argument('--local', action='store_true')
    p.add_argument('--local-model', default='nessa-lfm-32k:latest')
    p.add_argument('--base', default='http://127.0.0.1:11435')
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).read_bytes()
    (a.out/'runner.py').write_bytes(source)
    manifest = dict(model=a.model, local_model=a.local_model, cloud=a.cloud, local=a.local,
                    cloud_max_tokens=128, local_max_tokens=32, temperature=0,
                    python=platform.python_version(), script_sha256=hashlib.sha256(source).hexdigest())
    (a.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    rows = []
    def save(label, result):
        rows.append(dict(case=label, **result))
        (a.out/'results.json').write_text(json.dumps(rows, indent=2)+'\n')
        print(label, json.dumps(result), flush=True)
    short = 'List the integers from 1 to 80 separated by commas. No explanation.'
    prefix = 'Reference notes:\n' + '\n'.join(f'Entry {i}: keep operation receipts and verify results.' for i in range(200))
    long = prefix+'\n\n'+short
    if a.cloud:
        for rep in range(2):
            for label, prompt in [('short', short), ('long', long), ('repeat_prefix', long),
                                  ('changed_prefix', f'Run {rep}: new context.\n'+long)]:
                save(f'{label}_{rep}', cloud_probe(a.model, prompt))
        for workers in (1, 2):
            start = time.monotonic()
            with ThreadPoolExecutor(workers) as pool:
                results = list(pool.map(lambda _: cloud_probe(a.model, short), range(4)))
            for i, result in enumerate(results):
                save(f'concurrency_{workers}_{i}', result)
            save(f'batch_{workers}', dict(wall_seconds=time.monotonic()-start, requests=4,
                                         successful=sum(r['status']=='ok' for r in results)))
    if a.local:
        for route in ('version', 'ps'):
            with urllib.request.urlopen(a.base+'/api/'+route, timeout=10) as r:
                (a.out/(route+'.json')).write_text(json.dumps(json.load(r), indent=2))
        for label in ('local_first', 'local_repeat'):
            save(label, local_probe(a.base, a.local_model, short))


if __name__ == '__main__':
    main()
