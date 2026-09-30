'use strict';
const $ = id => document.getElementById(id);
let runs = [], selected = null, busy = false, generation = 0, lastDetail = null;
const node = (tag, text, cls) => { const el = document.createElement(tag); if (text !== undefined && text !== null) el.textContent = text; if (cls) el.className = cls; return el; };
const human = value => typeof value === 'string' ? value.replaceAll('_', ' ') : 'Unavailable';
const good = new Set(['verified', 'passed']);
const bad = new Set(['failed', 'failed_checks', 'error', 'blocked', 'rejected', 'setup_error', 'timeout']);
function badge(status) { return node('span', human(status || 'unavailable'), `badge ${good.has(status) ? 'good' : bad.has(status) ? 'bad' : 'warn'}`); }
function date(value) { return typeof value === 'number' ? new Date(value * 1000).toLocaleString() : 'Time unavailable'; }
function empty(parent, text = 'Unavailable in recorded evidence') { parent.append(node('p', text, 'empty')); }
function list(parent, values, ordered = false) { if (values === null || values === undefined) return empty(parent); if (!values.length) return empty(parent, 'None recorded'); const ul = node(ordered ? 'ol' : 'ul'); values.forEach(value => ul.append(node('li', value))); parent.append(ul); }
function card(title) { const el = node('section', null, 'card'); el.append(node('h3', title)); return el; }
function label(parent, title) { parent.append(node('div', title, 'label')); }
function record(parent, title, value) { label(parent, title); if (value === null || value === undefined) empty(parent); else parent.append(node('pre', JSON.stringify(value, null, 2), 'record')); }
function statusItem(parent, title, value, detail) { const item = node('div'); item.append(node('span', title, 'caption'), badge(value)); if (detail) item.append(node('div', detail, 'evidence-note')); parent.append(item); }
async function api(path) { const response = await fetch(path, {cache: 'no-store', credentials: 'omit'}); if (!response.ok) throw new Error(`Evidence request failed (${response.status})`); return response.json(); }
function renderRuns() {
  const container = $('runs'); container.replaceChildren();
  const query = $('search').value.toLowerCase();
  const filtered = runs.filter(r => [r.name, r.model, r.status].join(' ').toLowerCase().includes(query));
  $('run-count').textContent = `${filtered.length} / ${runs.length}`;
  if (!filtered.length) return empty(container, runs.length ? 'No matching runs' : 'No recorded runs found in this root');
  for (const run of filtered) {
    const button = node('button', null, `run${run.id === selected ? ' selected' : ''}`); button.type = 'button'; button.setAttribute('aria-current', run.id === selected ? 'true' : 'false');
    button.append(node('span', run.name, 'run-title'));
    const meta = node('span', null, 'run-meta'); meta.append(badge(run.status)); button.append(meta);
    button.append(node('span', run.model || 'Model unavailable', 'run-model'));
    button.addEventListener('click', () => { selectRun(run.id); $('follow').checked = false; renderRuns(); loadDetail(); });
    container.append(button);
  }
}
function renderDetail(r) {
  // All evidence is untrusted text. Never use innerHTML, links, or executable markup.
  const main = $('main'), content = node('div', null, 'content');
  content.append(node('span', 'RECORDED RUN EVIDENCE', 'eyebrow'), node('h2', r.name));
  content.append(node('div', `${r.model || 'Model unavailable'} · ${r.mode || 'Mode unavailable'}${r.solve_mode ? ` / ${r.solve_mode}` : ''} · Updated ${date(r.updated)}`, 'metadata'));
  if (r.warnings.length) { const warning = node('div', null, 'notice'); list(warning, r.warnings); content.append(warning); }
  const strip = node('section', null, 'status-strip');
  statusItem(strip, 'CONTROLLER STATUS', r.status, r.status_source ? `Source: ${r.status_source}` : 'No final controller result recorded');
  statusItem(strip, 'INDEPENDENT GRADE', r.independent?.status, r.independent ? 'Recorded separately from controller checks' : 'Audit absent or not recorded');
  const process = node('div'); process.append(node('span', 'PROCESS EXIT', 'caption'), node('span', r.process_exit_code === null ? 'Unavailable' : String(r.process_exit_code), 'value'), node('div', 'Process exit is not verification', 'evidence-note')); strip.append(process); content.append(strip);
  if (r.status === 'in_progress_or_interrupted') content.append(node('div', 'No final result yet. Events may belong to an active or interrupted run; this viewer does not infer process liveness.', 'notice'));
  const stages = node('div', null, 'stage-row'); ['intake', 'solve', 'respond'].forEach((name, i) => { const recorded = r.stages.includes(name); const step = node('div', null, `stage ${recorded ? 'recorded' : ''}`); step.append(node('span', `0${i + 1}`, 'num'), node('strong', human(name)), node('small', recorded ? (name === r.last_stage ? 'Last observed stage' : 'Stage entry recorded') : 'Not recorded')); stages.append(step); }); content.append(stages);
  const task = card('Requested task'); task.append(node('p', r.task || 'Task unavailable in recorded evidence')); content.append(task);
  const grid = node('div', null, 'grid');
  const plan = card('Plan & approval'); plan.append(badge(r.approved === true ? 'approved' : r.approved === false ? 'rejected' : 'approval_unavailable'));
  if (r.plan) { plan.append(node('p', r.plan.goal)); list(plan, r.plan.steps, true); label(plan, 'Planned file scope'); list(plan, r.plan.files); label(plan, 'Requested checks'); list(plan, r.plan.checks); } else empty(plan, 'No plan recorded'); grid.append(plan);
  const changes = card('Actual changed files'); list(changes, r.changed_files); changes.append(node('p', r.changed_source ? `Source: ${r.changed_source}` : 'No final change list or checkpoint recorded', 'evidence-note')); changes.append(node('p', 'The planned file list is not proof of an edit.', 'evidence-note'));
  label(changes, 'Independent integrity evidence');
  for (const [key, value] of Object.entries(r.integrity)) { if (value !== null) changes.append(node('div', `${human(key)}: ${value ? 'true' : 'false'}`, 'check-detail')); }
  if (Object.values(r.integrity).every(v => v === null)) empty(changes);
  grid.append(changes); content.append(grid);
  const checkGrid = node('div', null, 'grid');
  for (const phase of ['baseline', 'final']) { const section = card(`${human(phase)} checks`); const entries = Object.entries(r.checks[phase]); if (!entries.length) empty(section, 'No checks recorded for this phase'); entries.forEach(([name, check]) => { const row = node('div', null, 'check'), heading = node('div', null, 'check-heading'); heading.append(node('strong', name), badge(check.status)); row.append(heading); if (check.recorded) row.append(node('div', check.recorded, 'check-detail')); else row.append(node('div', `Exit: ${check.exit_code ?? 'unavailable'} · Time: ${check.seconds === null || check.seconds === undefined ? 'unavailable' : `${check.seconds.toFixed(3)}s`}`, 'check-detail')); if (check.counts && Object.keys(check.counts).length) row.append(node('div', Object.entries(check.counts).map(([k, v]) => `${k}: ${v}`).join(' · '), 'check-detail')); section.append(row); }); checkGrid.append(section); } content.append(checkGrid);
  const metrics = card('Measured timing & usage'), measurements = node('div', null, 'metrics');
  for (const [key, value] of Object.entries(r.metrics)) { const item = node('div', null, 'metric'); let text = 'Unavailable'; if (value !== null) text = key.endsWith('seconds') ? `${value.toFixed(2)}s` : key.endsWith('kib') ? `${(value / 1024).toFixed(0)} MiB` : value.toLocaleString(); item.append(node('span', text, 'value'), node('span', human(key), 'label')); measurements.append(item); }
  metrics.append(measurements, node('p', 'Tokens come from the recorded audit. Missing measurements stay unavailable.', 'evidence-note')); content.append(metrics);
  const patch = card('Patch review'); patch.append(node('p', r.patch_source ? `Source: ${r.patch_source}. Display only; nothing is applied.` : 'No patch artifact available', 'evidence-note'));
  if (r.patch === null) empty(patch); else if (!r.patch) empty(patch, 'Recorded patch is empty. No patch changes to review.'); else { const pre = node('pre', null, 'patch'); for (const line of r.patch.split('\n')) pre.append(node('span', line || ' ', `diff-line ${line.startsWith('+++') || line.startsWith('---') ? '' : line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : line.startsWith('@@') ? 'diff-hunk' : ''}`)); patch.append(pre); } content.append(patch);
  const audit = card('Independent audit & replay'); record(audit, 'Independent grade', r.independent); record(audit, 'Patch replay', r.replay); record(audit, 'Replay tests', r.replay_tests); record(audit, 'Independent replay grade', r.independent_replay); content.append(audit);
  if (r.failures.length) { const failures = card('Recorded failures & rejections'); r.failures.forEach(f => { const entry = node('div', null, 'failure'); entry.append(node('small', `${human(f.event)}${f.phase ? ` · ${f.phase}` : ''}`), node('span', f.message || 'No diagnostic message recorded')); failures.append(entry); }); content.append(failures); }
  if (r.advisory) { const section = card('Model narration · advisory only'); section.append(node('p', 'This text does not determine controller status, checks, or completion.', 'evidence-note'), badge(r.advisory.status)); section.append(node('p', r.advisory.text || r.advisory.error || 'No narration text recorded')); content.append(section); }
  const evidence = card('Evidence inspected'); list(evidence, r.evidence_files); evidence.append(node('p', 'Only known evidence files and selected fields are read. Transcripts, raw model reasoning, environment dumps, and arbitrary project files are not served.', 'evidence-note'));
  const disclosure = node('details'); disclosure.append(node('summary', 'Recorded stage, approval, and checkpoint events')); record(disclosure, 'Timeline', r.timeline); evidence.append(disclosure); content.append(evidence);
  // Preserve the reader's scroll position and expanded details across polling.
  const oldScroll = main.scrollTop, openDetails = [...main.querySelectorAll('details')].map(d => d.open); main.replaceChildren(content); [...main.querySelectorAll('details')].forEach((d, i) => d.open = openDetails[i] || false); main.scrollTop = oldScroll;
}
function selectRun(id) { if (id === selected) return; selected = id; generation++; lastDetail = null; $('main').replaceChildren(node('p', 'Reading selected run evidence…', 'empty')); $('main').scrollTop = 0; }
async function loadDetail() { if (!selected) return; const id = selected, version = generation; try { const detail = await api(`/api/runs/${id}`); if (id !== selected || version !== generation) return; const serialized = JSON.stringify(detail); if (lastDetail !== serialized) { renderDetail(detail); lastDetail = serialized; } } catch (error) { if (id === selected && version === generation) { $('connection').textContent = `${error.message}; showing last evidence`; $('connection').className = 'stale'; if (!lastDetail) { $('main').replaceChildren(node('div', error.message, 'notice')); } } } }
async function refresh() {
  if (busy) return; busy = true; $('refresh').disabled = true;
  try { const data = await api('/api/runs'); runs = data.runs; if ($('follow').checked || !selected) { const next = runs[0]?.id || null; selectRun(next); }
    $('list-warning').classList.toggle('hidden', !data.truncated); $('list-warning').textContent = 'Discovery limit reached. Use a narrower runs root to see older or deeper runs.';
    $('connection').textContent = `Local · refreshed ${new Date().toLocaleTimeString()}`; $('connection').className = ''; renderRuns();
    if (selected) await loadDetail(); else $('main').replaceChildren(node('div', 'No runs found. This viewer shows only existing evidence; it never creates demo data.', 'welcome'));
  } catch (error) { $('connection').textContent = `${error.message}; retrying`; $('connection').className = 'stale'; if (!runs.length) $('runs').replaceChildren(node('p', error.message, 'notice')); }
  finally { busy = false; $('refresh').disabled = false; }
}
$('search').addEventListener('input', renderRuns);
$('refresh').addEventListener('click', refresh);
$('follow').addEventListener('change', refresh);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 10000);
refresh();
