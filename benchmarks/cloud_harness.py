"""Small live cloud/harness experiment. Requires explicit --cloud; writes private runs.

This is a synthetic integration pilot, not a general coding benchmark or an equal-compute
ablation. Model-only gets one response; Nessa gets an iterative tool loop. Hidden checks
are withheld from prompts/tools and executed only after each attempt. No training capture.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentharness.agent import Agent, AgentConfig
from agentharness.checks import CheckResult, CheckRunner, syntax_check
from agentharness.cloud import DEFAULT_MODELS, HF_ROUTER, FallbackClient, load_token
from agentharness.llm import ChatClient
from agentharness.workspace import Workspace


TASKS = [
    dict(id='pagination', prompt='Repair paginate(items, page, size). Pages are one-based. '
         'Reject non-integer or boolean page/size with TypeError; reject non-positive values '
         'with ValueError. Return a list slice, including [] beyond the last page, and never '
         'mutate the input. Preserve the function signature.',
         files={'pagination.py': 'def paginate(items, page, size):\n    start = page * size\n    return items[start:start + size]\n'},
         public="from pagination import paginate\nassert paginate([1,2,3,4,5], 1, 2) == [1,2]\nassert paginate([1,2,3,4,5], 2, 2) == [3,4]\n",
         hidden="""from pagination import paginate
items = list(range(9))
for size in (1,2,4,20):
    for page in range(1,15):
        assert paginate(items,page,size) == items[(page-1)*size:page*size]
assert items == list(range(9))
assert paginate([],1,2) == []
for page,size in ((0,2),(-1,2),(1,0),(1,-3)):
    try: paginate(items,page,size)
    except ValueError: pass
    else: raise AssertionError('non-positive argument accepted')
for page,size in ((True,2),(1,False),(1.0,2),(1,2.0),('1',2)):
    try: paginate(items,page,size)
    except TypeError: pass
    else: raise AssertionError('invalid argument type accepted')
""",
         gold={'pagination.py': "def paginate(items, page, size):\n    if type(page) is not int or type(size) is not int:\n        raise TypeError()\n    if page < 1 or size < 1:\n        raise ValueError()\n    return list(items[(page-1)*size:page*size])\n"}),
    dict(id='dependencies', prompt='Implement build_order(graph) where graph maps task names '
         'to lists of prerequisite names. Return every node (including prerequisites absent '
         'as keys) once, prerequisites first. At each step choose the lexicographically '
         'smallest currently ready node. Ignore duplicate edges, do not mutate graph, and '
         'raise ValueError for any cycle including a self-cycle. Empty graph returns [].',
         files={'deps.py': 'def build_order(graph):\n    return sorted(graph)\n'},
         public="from deps import build_order\nassert build_order({'app':['lib'],'lib':[]}) == ['lib','app']\nassert build_order({}) == []\n",
         hidden="""from deps import build_order
import copy
g = {'a':['c','c'],'b':[],'d':['a']}
before = copy.deepcopy(g)
assert build_order(g) == ['b','c','a','d']
assert g == before
assert build_order({'z':['x']}) == ['x','z']
assert build_order({'a':[],'b':[],'c':['a']}) == ['a','b','c']
for g in ({'x':['x']}, {'a':['b'],'b':['a']}, {'ok':[], 'a':['b'],'b':['a']}):
    try: build_order(g)
    except ValueError: pass
    else: raise AssertionError('cycle accepted')
""",
         gold={'deps.py': "def build_order(graph):\n    pending = {n:set(ds) for n,ds in graph.items()}\n    for ds in list(pending.values()):\n        for n in ds:\n            pending.setdefault(n,set())\n    result = []\n    while pending:\n        ready = sorted(n for n,ds in pending.items() if not ds)\n        if not ready:\n            raise ValueError('cycle')\n        node = ready[0]\n        result.append(node)\n        del pending[node]\n        for ds in pending.values():\n            ds.discard(node)\n    return result\n"}),
    dict(id='invoice', prompt='Fix invoice_total(csv_text) across invoice.py and money.py. '
         'The CSV has sku,quantity,unit_price columns; use CSV quoting correctly, ignore '
         'blank rows, trim surrounding whitespace, require nonnegative integer quantity '
         'and finite nonnegative decimal unit_price, and raise ValueError for invalid rows '
         'or missing required columns. Round each line to cents using ROUND_HALF_UP, then '
         'sum lines. Return a Decimal, including Decimal("0.00") for a header-only CSV. '
         'Keep line_total(quantity, unit_price) usable independently with the same validation.',
         files={'money.py': 'def line_total(quantity, unit_price):\n    return round(float(quantity) * float(unit_price), 2)\n',
                'invoice.py': 'from money import line_total\ndef invoice_total(csv_text):\n    rows = csv_text.strip().splitlines()[1:]\n    return sum(line_total(*row.split(",")[1:]) for row in rows)\n'},
         public="from invoice import invoice_total\nfrom decimal import Decimal\nassert invoice_total('sku,quantity,unit_price\\na,2,1.25\\n') == Decimal('2.50')\n",
         hidden="""from invoice import invoice_total
from money import line_total
from decimal import Decimal
assert line_total('1','0.005') == Decimal('0.01')
assert isinstance(line_total('2','1.00'), Decimal)
assert invoice_total('sku,quantity,unit_price\\n"a,b",1,0.005\\nc,1,0.005\\n') == Decimal('0.02')
assert invoice_total('sku,quantity,unit_price\\n\\na, 2 , 1.235 \\n') == Decimal('2.47')
assert invoice_total('sku,quantity,unit_price\\n') == Decimal('0.00')
assert isinstance(invoice_total('sku,quantity,unit_price\\n'), Decimal)
for q,p in (('-1','1'),('1.5','1'),('1','NaN'),('1','Infinity'),('1','-0.01'),('x','1')):
    try: line_total(q,p)
    except ValueError: pass
    else: raise AssertionError('bad numeric input accepted')
for text in ('wrong,headers\\nx,y\\n','sku,quantity,unit_price\\na,1\\n'):
    try: invoice_total(text)
    except ValueError: pass
    else: raise AssertionError('bad CSV accepted')
""",
         gold={'money.py': "from decimal import Decimal, InvalidOperation, ROUND_HALF_UP\nimport re\ndef line_total(quantity, unit_price):\n    q = str(quantity).strip()\n    if not re.fullmatch(r'[0-9]+',q):\n        raise ValueError('quantity')\n    try:\n        p = Decimal(str(unit_price).strip())\n        if not p.is_finite() or p < 0:\n            raise ValueError('price')\n        return (int(q)*p).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)\n    except InvalidOperation as e:\n        raise ValueError('price') from e\n",
               'invoice.py': "import csv, io\nfrom decimal import Decimal\nfrom money import line_total\ndef invoice_total(csv_text):\n    rows = csv.DictReader(io.StringIO(csv_text))\n    if not {'sku','quantity','unit_price'}.issubset(rows.fieldnames or []):\n        raise ValueError('headers')\n    total = Decimal('0.00')\n    for row in rows:\n        if None in row or any(row.get(k) is None for k in ('sku','quantity','unit_price')):\n            raise ValueError('row')\n        total += line_total(row['quantity'],row['unit_price'])\n    return total\n"}),
]


def write_files(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content)


def check(root, script):
    started = time.monotonic()
    # -I avoids user site/custom startup; model code still runs as ordinary Python.
    code = 'import sys\nsys.path.insert(0, sys.argv[1])\n' + script
    try:
        p = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(root)],
                           cwd=root, capture_output=True, text=True, timeout=10)
        return dict(passed=p.returncode == 0, exit_code=p.returncode,
                    output=(p.stdout+p.stderr)[-4000:], seconds=time.monotonic()-started)
    except subprocess.TimeoutExpired:
        return dict(passed=False, exit_code=None, output='grader timeout', seconds=10)


class MeteredClient(ChatClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def _request(self, route, body=None, timeout=None):
        return self._meter(lambda: super(MeteredClient, self)._request(route, body, timeout))

    def _stream_request(self, body):
        return self._meter(lambda: super(MeteredClient, self)._stream_request(body))

    def _meter(self, request):
        started = time.monotonic()
        row = dict(requested_model=self.model)
        try:
            data = request()
            row.update(served_model=data.get('model'), usage=data.get('usage', {}), success=True)
            return data
        except Exception as exc:
            row.update(success=False, error=str(exc).replace(self.api_key, '<redacted>')[:400])
            raise
        finally:
            row['seconds'] = round(time.monotonic()-started, 3)
            self.calls.append(row)


def run_attempt(task, model, arm, root, token, stream=False):
    source = root / 'source'
    write_files(source, task['files'])
    ws = Workspace.create(source, root / 'work')
    remote = MeteredClient(HF_ROUTER, model, api_key=token, allow_remote=True,
                           max_tokens=4096, timeout=60, retries=1, temperature=0, stream=stream)
    local = MeteredClient('http://127.0.0.1:11435/v1', 'nessa-lfm-32k:latest',
                          max_tokens=1024, timeout=20, retries=1, temperature=0)
    client = FallbackClient([remote, local])
    result = dict(task=task['id'], model=model, arm=arm)
    started = time.monotonic()
    try:
        if arm == 'raw':
            messages = [dict(role='system', content='Solve the programming task. Return only a JSON '
                'object mapping changed filenames to their complete new file contents. No Markdown.'),
                dict(role='user', content=task['prompt']+'\nFiles:\n'+json.dumps(task['files'])+
                     '\nPublic checks:\n'+task['public'])]
            reply = client.chat(messages)
            (root/'reply.txt').write_text(reply.content)
            content = reply.content.strip()
            if content.startswith('```'):
                content = content.split('\n',1)[1].rsplit('```',1)[0]
            files = json.loads(content)
            if not isinstance(files,dict) or not files or any(k not in task['files'] or not isinstance(v,str) for k,v in files.items()):
                raise ValueError('Response must map existing task filenames to complete source strings')
            write_files(ws.repo, files)
            result['status'] = 'submitted'
        else:
            def public(workspace):
                r = check(workspace.repo, task['public'])
                return CheckResult('tests', 'passed' if r['passed'] else 'failed',
                                   exit_code=r['exit_code'], output=r['output'], seconds=r['seconds'])
            config = AgentConfig(require_approval=False, max_steps=16, plan_steps=6,
                                 time_budget=240, command_timeout=10, allow_shell=False,
                                 allow_extract=False, allow_mcp=False, allow_subagents=False)
            def event(name, data):
                if name in ('tool','end'):
                    print(json.dumps(dict(event=name, task=task['id'], model=model,
                                          tool=data.get('name'), status=data.get('status'))), flush=True)
            agent = Agent(client, ws, config=config, lessons=[], on_event=event,
                          checks=CheckRunner({'syntax':syntax_check,'tests':public}))
            outcome = agent.run(task['prompt']+'\nPublic checks:\n'+task['public'])
            (root/'agent-result.json').write_text(json.dumps(outcome.to_dict(),indent=2))
            result.update(status=outcome.status, steps=outcome.steps)
    except Exception as exc:
        result.update(status='error', error=str(exc).replace(token,'<redacted>')[:1000])
    result.update(seconds=round(time.monotonic()-started,2), cloud_calls=remote.calls,
                  local_calls=local.calls, fallback_used=bool(local.calls),
                  cloud_answered=any(c['success'] for c in remote.calls),
                  hidden=check(ws.repo,task['public']+task['hidden']),
                  source_unchanged=all((source/k).read_text()==v for k,v in task['files'].items()),
                  changed_files=ws.changed_files())
    result['cloud_pass'] = (result['hidden']['passed'] and result['cloud_answered']
                            and not result['fallback_used'] and result['source_unchanged']
                            and result['status'] != 'error')
    (root/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({k:result[k] for k in ('task','model','arm','status','seconds','cloud_pass','fallback_used')}),flush=True)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cloud',action='store_true',required=True)
    p.add_argument('--models',nargs='+',default=list(DEFAULT_MODELS))
    p.add_argument('--arms',nargs='+',choices=('raw','nessa'),default=['raw','nessa'])
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--stream',action='store_true')
    args = p.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    # Reject defective fixtures before spending any inference credits.
    for task in TASKS:
        base = args.out/'fixture-validation'/task['id']
        write_files(base/'broken',task['files'])
        write_files(base/'gold',task['gold'])
        assert not check(base/'broken',task['public']+task['hidden'])['passed'], task['id']
        assert check(base/'gold',task['public']+task['hidden'])['passed'], task['id']
    token = load_token()
    jobs = [(task,model,arm) for model in args.models for task in TASKS for arm in args.arms]
    random.Random(20261008).shuffle(jobs)
    manifest = dict(stream=args.stream, models=args.models, arms=args.arms, task_ids=[t['id'] for t in TASKS],
                    script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    note='Synthetic single-repeat pilot; raw is one response, Nessa is iterative. '
                         'No basic-agent arm. Different compute budgets; no causal harness uplift claim.',
                    cloud_output_limit=4096, cloud_timeout=60, harness_steps=16,
                    harness_time_budget=240, reasoning_effort='provider default',temperature=0)
    (args.out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    rows=[]
    for i,(task,model,arm) in enumerate(jobs):
        print(f'RUN {i+1}/{len(jobs)} {model} {task["id"]} {arm}',flush=True)
        rows.append(run_attempt(task,model,arm,args.out/f'{i+1:02d}-{task["id"]}-{arm}',token, stream=args.stream))
        (args.out/'results.json').write_text(json.dumps(rows,indent=2))
    print('RESULTS '+str(args.out/'results.json'),flush=True)


if __name__ == '__main__':
    main()
