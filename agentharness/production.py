"""Persistent production DAG, artifact versions and measured workflow trials.

One controller dispatches bounded jobs. No background autonomous agents or automatic
code/weight changes. Reviews remain attributed notes, separate from machine checks.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import platform
import sys
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import uuid
from .checks import run_command
from .session import atomic_json
from .workspace import ToolError


def fingerprint(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):
        raise ToolError('IDs require 1–64 letters, digits, underscores or hyphens.')
    return value


def argv(value):
    if not isinstance(value,list) or not value or len(value)>80 or any(
            not isinstance(v,str) or len(v)>8000 or '\0' in v for v in value):
        raise ToolError('Commands require a bounded nonempty argv array; shell strings are not supported.')
    return value


class Production:
    def __init__(self,root):
        self.root=Path(root).resolve()
        self.path=self.local('production/state.json')

    def local(self,name):
        if not isinstance(name,str) or not name or Path(name).is_absolute():
            raise ToolError('Use a project-relative path.')
        path=(self.root/name).resolve()
        if not path.is_relative_to(self.root) or path==self.root:
            raise ToolError('Path must stay inside the project.')
        return path

    def read(self):
        if not self.path.exists():
            return dict(version=1,brief={},assets={},tasks={},runs=[],reviews=[],trials=[])
        if self.path.stat().st_size>4_000_000:
            raise ToolError('Production state exceeds 4 MB; archive old runs before continuing.')
        data=json.loads(self.path.read_text())
        if data.get('version')!=1:
            raise ToolError('Unsupported production state version.')
        return data

    @contextmanager
    def edit(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        lock=self.local('production/state.lock')
        with lock.open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            state=self.read()
            yield state
            if len(json.dumps(state))>4_000_000:
                raise ToolError('Production state limit reached; archive old runs.')
            atomic_json(self.path,state)

    def update(self,operation,data):
        if not isinstance(data,dict):
            raise ToolError('data must be an object.')
        with self.edit() as state:
            if operation=='brief':
                if len(json.dumps(data))>12000:
                    raise ToolError('Brief must fit within 12,000 characters.')
                state['brief']=data
            elif operation=='asset':
                key=identifier(data.get('id'))
                path=self.local(data.get('path'))
                if not path.is_file():raise ToolError('Asset file does not exist.')
                versions=state['assets'].get(key,[])
                digest=fingerprint(path)
                if not versions or versions[-1]['sha256']!=digest or versions[-1]['path']!=data['path']:
                    snapshot=self.local('production/assets/'+digest+path.suffix)
                    snapshot.parent.mkdir(parents=True,exist_ok=True)
                    if not snapshot.exists():
                        temporary=snapshot.with_name(snapshot.name+'.tmp')
                        shutil.copyfile(path,temporary)
                        if fingerprint(temporary)!=digest:
                            temporary.unlink()
                            raise ToolError('Asset changed during registration; retry after saving it.')
                        temporary.replace(snapshot)
                    versions.append(dict(path=data['path'],snapshot=str(snapshot.relative_to(self.root)),
                        sha256=digest,bytes=path.stat().st_size,
                        version=len(versions)+1,role=str(data.get('role','asset'))[:100],
                        notes=str(data.get('notes',''))[:1500],time=time.time()))
                state['assets'][key]=versions
            elif operation=='task':
                key=identifier(data.get('id'))
                if key in state['tasks'] and any(r['task']==key and r['status']=='running' and not r.get('recovered_at') for r in state['runs']):
                    raise ToolError('Cannot replace a running task.')
                task=dict(id=key,argv=argv(data.get('argv')),dependencies=data.get('dependencies',[]),
                          assets=data.get('assets',[]),outputs=data.get('outputs',[]),
                          checks=data.get('checks',[]),timeout=data.get('timeout',120),
                          purpose=str(data.get('purpose',''))[:1000])
                if not isinstance(task['timeout'],int) or not 1<=task['timeout']<=600:
                    raise ToolError('Timeout must be 1–600 seconds for the entire job including checks.')
                for field in ('dependencies','assets','outputs','checks'):
                    if not isinstance(task[field],list) or len(task[field])>32:
                        raise ToolError(f'{field} must be a list with at most 32 entries.')
                if not task['outputs']:raise ToolError('A production task needs expected artifact paths.')
                for p in task['outputs']:self.local(p)
                for check in task['checks']:argv(check)
                for dep in task['dependencies']:
                    identifier(dep)
                    if dep not in state['tasks']:raise ToolError(f'Unknown dependency: {dep}')
                for asset in task['assets']:
                    if asset not in state['assets']:raise ToolError(f'Unknown asset: {asset}')
                if key not in state['tasks'] and len(state['tasks'])>=128:
                    raise ToolError('A production supports up to 128 tasks.')
                state['tasks'][key]=task
                self.order(state)  # reject cycles before committing
            elif operation=='review':
                path=self.local(data.get('path'))
                if not path.is_file():raise ToolError('Reviewed artifact not found.')
                verdict=data.get('verdict')
                if verdict not in ('needs_work','accepted','unreviewed'):
                    raise ToolError('Review verdict must be needs_work, accepted or unreviewed.')
                state['reviews'].append(dict(path=data['path'],sha256=fingerprint(path),
                    verdict=verdict,notes=str(data.get('notes',''))[:3000],
                    source='agent_recorded_note',time=time.time()))
            else:
                raise ToolError('operation must be brief, asset, task or review.')
        return self.status()

    def order(self,state):
        ordered,active,done=[],set(),set()
        def visit(key):
            if key in active:raise ToolError('Task dependency cycle.')
            if key in done:return
            if key not in state['tasks']:raise ToolError(f'Unknown dependency: {key}')
            active.add(key)
            for dep in state['tasks'][key]['dependencies']:visit(dep)
            active.remove(key);done.add(key);ordered.append(key)
        for key in state['tasks']:visit(key)
        return ordered

    def signature(self,state,key):
        task=state['tasks'][key]
        versions={name:state['assets'][name][-1] for name in task['assets']}
        for name,version in versions.items():
            path=self.local(version['path'])
            if not path.is_file() or fingerprint(path)!=version['sha256']:
                raise ToolError(f'Asset {name} changed or disappeared; register its new version.')
        # Dependency identity includes its current task and input asset revisions.
        deps={}
        for dep in task['dependencies']:
            completed=self.current(state,dep)
            deps[dep]=dict(signature=self.signature(state,dep),outputs=completed['outputs'] if completed else None)
        value=dict(task=task,assets=versions,dependencies=deps)
        return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

    def current(self,state,key):
        try: sig=self.signature(state,key)
        except ToolError:return None
        for row in reversed(state['runs']):
            if row['task']==key and row.get('signature')==sig and row['status'] in ('completed','verified'):
                if all(self.local(p).is_file() and fingerprint(self.local(p))==h for p,h in row['outputs'].items()):
                    return row
        return None

    def status(self):
        state=self.read(); rows=[]
        for key in self.order(state):
            active=next((r for r in reversed(state['runs']) if r['task']==key and r['status'] in ('running','outcome_unknown') and not r.get('recovered_at')),None)
            current=self.current(state,key)
            ready=all(self.current(state,d) for d in state['tasks'][key]['dependencies'])
            try:self.signature(state,key);valid=True
            except ToolError:valid=False
            latest=next((r for r in reversed(state['runs']) if r['task']==key),None)
            rows.append(dict(id=key,argv=state['tasks'][key]['argv'],
                             last_run=latest['status'] if latest else None,status='running_or_interrupted' if active else current['status'] if current
                             else 'ready' if ready and valid else 'blocked',dependencies=state['tasks'][key]['dependencies']))
        return dict(brief=state['brief'],assets={k:dict(version=v[-1]['version'],path=v[-1]['path'],snapshot=v[-1].get('snapshot')) for k,v in state['assets'].items()},
                    tasks=rows,runs=len(state['runs']),reviews=len(state['reviews']),trials=state['trials'][-5:])

    def run(self,key):
        key=identifier(key)
        with self.edit() as state:
            if key not in state['tasks']:raise ToolError('Unknown task.')
            self.order(state)
            if any(r['status'] in ('running','outcome_unknown') and not r.get('recovered_at') for r in state['runs']):
                raise ToolError('Another job is running or was interrupted. Inspect its run record before recovery.')
            task=state['tasks'][key]
            if not all(self.current(state,d) for d in task['dependencies']):
                raise ToolError('Dependencies are incomplete or their artifacts are stale.')
            signature=self.signature(state,key)
            run_id=uuid.uuid4().hex
            executables={}
            for command in [task['argv']]+task['checks']:
                binary=shutil.which(command[0])
                if binary and Path(binary).is_file():
                    executables[str(Path(binary).resolve())]=fingerprint(Path(binary))
            environment=dict(platform=platform.platform(),python=sys.version,executables=executables)
            row=dict(id=run_id,task=key,signature=signature,environment=environment,status='running',started=time.time(),
                     outputs={},checks=[],seconds=None,definition=task)
            state['runs'].append(row)
        start=time.monotonic();results=[];outputs={};status='failed';interrupted=False
        try:
            previous={p:(self.local(p).stat().st_mtime_ns,fingerprint(self.local(p)))
                      for p in task['outputs'] if self.local(p).is_file()}
            code,out,timeout=run_command(shlex.join(task['argv']),self.root,timeout=task['timeout'])
            results.append(dict(kind='job',exit_code=code,timeout=timeout,output=out[-3000:]))
            if timeout:status='outcome_unknown'
            elif code==0:
                for path in task['outputs']:
                    file=self.local(path)
                    if not file.is_file() or not file.stat().st_size:raise ToolError(f'Missing/empty output: {path}')
                    outputs[path]=fingerprint(file)
                    if previous.get(path)==(file.stat().st_mtime_ns,outputs[path]):
                        raise ToolError(f'Output was not produced or refreshed by this job: {path}')
                for command in task['checks']:
                    remaining=task['timeout']-(time.monotonic()-start)
                    if remaining<=0:raise ToolError('Job time budget exhausted before checks.')
                    code,out,timeout=run_command(shlex.join(command),self.root,timeout=remaining)
                    results.append(dict(kind='check',argv=command,exit_code=code,timeout=timeout,output=out[-1500:]))
                    if timeout:status='outcome_unknown';break
                    if code!=0:break
                else:
                    if any(fingerprint(self.local(p))!=digest for p,digest in outputs.items()):
                        raise ToolError('A verification command changed an output artifact.')
                    status='verified' if task['checks'] else 'completed'
        except KeyboardInterrupt:
            status='outcome_unknown';interrupted=True
        except Exception as exc:
            results.append(dict(error=f'{type(exc).__name__}: {str(exc)[:500]}'))
        elapsed=time.monotonic()-start
        with self.edit() as state:
            row=next(r for r in state['runs'] if r['id']==run_id)
            row.update(status=status,seconds=elapsed,outputs=outputs,checks=results,finished=time.time())
        if interrupted:
            raise KeyboardInterrupt
        return row

    def recover(self,run_id,note):
        if not isinstance(note,str) or not 20<=len(note)<=3000:
            raise ToolError('Record what was inspected, whether the process stopped, and why retry is safe (20–3000 characters).')
        with self.edit() as state:
            row=next((r for r in state['runs'] if r['id']==run_id),None)
            if row is None or row['status'] not in ('running','outcome_unknown'):
                raise ToolError('Recovery requires an interrupted or unknown run ID.')
            row.update(recovered_at=time.time(),recovery_note=note,source='agent_recorded_recovery')
        return dict(run_id=run_id,status='retry_unblocked',note=note)

    def compare(self,baseline,candidate):
        """Record a measured timing comparison; never invent aesthetic scores or promote code."""
        with self.edit() as state:
            groups=[]
            for key in (baseline,candidate):
                if key not in state['tasks']:raise ToolError('Unknown comparison task.')
                sig=self.signature(state,key)
                rows=[r for r in state['runs'] if r['task']==key and r.get('signature')==sig]
                if not rows or any(r['status']!='verified' for r in rows):
                    raise ToolError('Each comparison arm needs successful runs with explicit checks.')
                groups.append(rows[-3:])
            a,b=state['tasks'][baseline],state['tasks'][candidate]
            if baseline==candidate or not a['checks'] or any(a[k]!=b[k] for k in ('checks','assets','dependencies','outputs')):
                raise ToolError('Use distinct tasks with the same checks, inputs, dependencies and output contract.')
            import statistics
            environments={json.dumps(r.get('environment'),sort_keys=True) for rows in groups for r in rows}
            if len(environments)!=1:
                raise ToolError('Comparison runs have different recorded runtime environments.')
            before,after=(statistics.median(r['seconds'] for r in rows) for rows in groups)
            trial=dict(baseline=baseline,candidate=candidate,samples=[len(r) for r in groups],
                       median_seconds=[before,after],speed_ratio=before/after if after else None,
                       status='timing_evidence' if all(len(r)>=3 for r in groups) else 'insufficient_repeats',
                       quality='explicit checks passed; visual/audio quality unassessed',
                       promoted=False,run_ids=[[r['id'] for r in rows] for rows in groups],time=time.time())
            state['trials'].append(trial)
        return trial


def status_tool(ctx,args):
    production=Production(ctx.ws.repo)
    if args.get('task'):
        state=production.read(); key=args['task']
        if key not in state['tasks']:raise ToolError('Unknown task.')
        result=dict(task=state['tasks'][key],runs=[r for r in state['runs'] if r['task']==key][-2:])
    else:result=production.status()
    return json.dumps(result,ensure_ascii=False)[:10000]


def update_tool(ctx,args):
    return json.dumps(Production(ctx.ws.repo).update(args['operation'],args['data']),ensure_ascii=False)[:10000]


def run_tool(ctx,args):
    row=Production(ctx.ws.repo).run(args['task'])
    prefix='TIMEOUT: ' if row['status']=='outcome_unknown' else 'ERROR: ' if row['status']=='failed' else ''
    return prefix+json.dumps(row,ensure_ascii=False)[:10000]


def compare_tool(ctx,args):
    return json.dumps(Production(ctx.ws.repo).compare(args['baseline'],args['candidate']))


def recover_tool(ctx,args):
    return json.dumps(Production(ctx.ws.repo).recover(args['run_id'],args['note']))
