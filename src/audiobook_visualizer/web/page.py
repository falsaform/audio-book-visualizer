"""The single-page web UI (self-contained HTML + CSS + JS).

Kept as one string so the server has no template/static build step. It talks to
the JSON API in ``server.py``: ``GET /api/state`` and the per-shot
``POST /api/scenes/{id}/regenerate``, ``/api/shots/{slug}/update`` and
``/api/shots/{slug}/split`` endpoints.
"""

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audiobook Visualizer</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #111; color: #eee; }
  header { padding: 1rem 1.5rem; border-bottom: 1px solid #333; display: flex;
           align-items: baseline; gap: 1rem; position: sticky; top: 0; background: #111; z-index: 5; }
  header h1 { margin: 0; font-size: 1.25rem; }
  header .sub { color: #999; font-size: .9rem; }
  header .badge { background: #1f2d3d; color: #7ad; padding: .25rem .6rem;
                  border-radius: 999px; font-size: .8rem; }
  header .badge.warn { background: #3d2f1f; color: #fc8; }
  header .spacer { margin-left: auto; }
  .layout { display: grid; grid-template-columns: 1fr 440px; gap: 0; height: calc(100vh - 58px); }
  .grid { overflow-y: auto; padding: 1.25rem; display: grid; gap: 1rem;
          grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); align-content: start; }
  .card { background: #1a1a1a; border-radius: 10px; overflow: hidden; cursor: pointer;
          border: 2px solid transparent; transition: border-color .15s; }
  .card:hover { border-color: #345; }
  .card.selected { border-color: #7ad; }
  .card img, .card .ph { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; background: #222; }
  .card .ph { display: flex; align-items: center; justify-content: center; color: #666; font-size: .85rem; }
  .card .cap { padding: .5rem .7rem; font-size: .85rem; }
  .card .cap .ch { color: #7aa; font-size: .72rem; }
  .card .cap .mv { color: #9c8; font-size: .7rem; text-transform: uppercase; letter-spacing: .03em; }
  aside { border-left: 1px solid #333; overflow-y: auto; padding: 1.25rem; background: #151515; }
  aside.empty { display: flex; align-items: center; justify-content: center; color: #666; text-align: center; }
  aside img.detail { width: 100%; border-radius: 8px; aspect-ratio: 16/9; object-fit: cover; background: #222; }
  aside h2 { font-size: 1.05rem; margin: .8rem 0 .2rem; }
  .slug { font-family: ui-monospace, monospace; font-size: .72rem; color: #678; }
  .meta { font-size: .85rem; color: #bbb; line-height: 1.5; }
  .meta b { color: #ddd; }
  .chips { display: flex; flex-wrap: wrap; gap: .35rem; margin: .4rem 0; }
  .chip { background: #233; color: #9cd; padding: .15rem .5rem; border-radius: 6px; font-size: .75rem; }
  .refs { display: flex; gap: .4rem; margin: .4rem 0; }
  .refs img { width: 48px; height: 48px; object-fit: cover; border-radius: 6px; border: 1px solid #345; }
  .source { background: #0e0e0e; border-left: 3px solid #345; border-radius: 0 6px 6px 0;
            padding: .55rem .7rem; font-size: .85rem; color: #cbd; line-height: 1.5;
            white-space: pre-wrap; max-height: 9rem; overflow-y: auto; }
  label { display: block; font-size: .78rem; color: #9aa; margin: .85rem 0 .25rem;
          text-transform: uppercase; letter-spacing: .04em; }
  textarea, input[type=text], select { width: 100%; background: #0e0e0e; color: #eee;
            border: 1px solid #333; border-radius: 6px; padding: .55rem; font-family: inherit; font-size: .85rem; }
  textarea { min-height: 90px; resize: vertical; }
  textarea#prompt { min-height: 120px; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: .6rem; }
  .btns { display: flex; gap: .5rem; }
  button { margin-top: .9rem; flex: 1; background: #2563eb; color: #fff; border: 0;
           border-radius: 7px; padding: .6rem; font-size: .9rem; cursor: pointer; }
  button.secondary { background: #2a2f3a; color: #cdd; }
  button:disabled { opacity: .6; cursor: progress; }
  .status { margin-top: .6rem; font-size: .82rem; min-height: 1.1em; }
  .status.err { color: #e88; }
  .status.ok { color: #8d8; }
  hr { border: 0; border-top: 1px solid #2a2a2a; margin: 1rem 0 .2rem; }
</style>
</head>
<body>
<header>
  <h1 id="title">Audiobook Visualizer</h1>
  <span class="sub" id="sub"></span>
  <span class="spacer"></span>
  <span class="badge warn" id="continuity" style="display:none"></span>
  <span class="badge" id="provider"></span>
</header>
<div class="layout">
  <div class="grid" id="grid"></div>
  <aside id="detail" class="empty">Select a shot to view its source, prompt and image.</aside>
</div>

<script>
const MOVES = ['', 'static', 'push_in', 'pull_out', 'pan_left', 'pan_right',
               'tilt_up', 'tilt_down', 'track_left', 'track_right'];
const MOVE_LABEL = { '': '(default — Ken Burns)' };
let STATE = null;
let SELECTED = null;

async function load() {
  const res = await fetch('/api/state');
  if (!res.ok) { document.getElementById('grid').innerHTML =
      '<p style="color:#e88">' + (await res.json()).detail + '</p>'; return; }
  STATE = await res.json();
  document.getElementById('title').textContent = STATE.title || 'Audiobook Visualizer';
  document.getElementById('sub').textContent = STATE.author ? '— ' + STATE.author : '';
  document.getElementById('provider').textContent = STATE.provider;
  const cont = document.getElementById('continuity');
  const n = (STATE.continuity || []).length;
  cont.style.display = n ? '' : 'none';
  cont.textContent = n ? `⚠ ${n} continuity note${n > 1 ? 's' : ''}` : '';
  cont.title = (STATE.continuity || []).map(c => `[${c.severity}] ${c.message}`).join('\n');
  renderGrid();
  if (SELECTED) {
    const s = STATE.scenes.find(s => s.id === SELECTED);
    if (s) renderDetail(s);
  }
}

function renderGrid() {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for (const s of STATE.scenes) {
    const card = document.createElement('div');
    card.className = 'card' + (s.id === SELECTED ? ' selected' : '');
    const media = s.image_url
      ? `<img loading="lazy" src="${s.image_url}" alt="">`
      : `<div class="ph">not generated</div>`;
    const move = s.camera_move ? `<span class="mv">▶ ${esc(s.camera_move)}</span>` : '';
    card.innerHTML = media +
      `<div class="cap"><div class="ch">${esc(s.chapter || s.scene_heading || '')}</div>` +
      `${esc(s.title || s.id)} ${move}</div>`;
    card.onclick = () => { SELECTED = s.id; renderGrid(); renderDetail(s); };
    grid.appendChild(card);
  }
}

function renderDetail(s) {
  const d = document.getElementById('detail');
  d.className = '';
  const chips = (s.characters_present || []).map(c => `<span class="chip">${esc(c)}</span>`).join('');
  const refs = (s.reference_urls || []).map(u => `<img src="${u}" title="reference">`).join('');
  const media = s.image_url ? `<img class="detail" src="${s.image_url}">`
    : `<div class="ph" style="aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;color:#666">not generated yet</div>`;
  const moveOpts = MOVES.map(m =>
    `<option value="${m}"${m === (s.camera_move || '') ? ' selected' : ''}>${esc(MOVE_LABEL[m] || m)}</option>`).join('');
  d.innerHTML = `
    ${media}
    <h2>${esc(s.title || s.id)}</h2>
    <div class="slug">${esc(s.scene_heading || '')} ${s.scene_heading ? '·' : ''} ${esc(s.id)}</div>
    <div class="meta">
      ${s.chapter ? `<div><b>Chapter:</b> ${esc(s.chapter)}</div>` : ''}
      ${s.summary ? `<div>${esc(s.summary)}</div>` : ''}
      ${s.setting ? `<div><b>Setting:</b> ${esc(s.setting)}</div>` : ''}
      <div>${[s.time_of_day, s.mood].filter(Boolean).map(esc).join(' · ')}</div>
      ${chips ? `<div class="chips">${chips}</div>` : ''}
      ${refs ? `<div><b>References:</b><div class="refs">${refs}</div></div>` : ''}
    </div>
    ${s.source_excerpt ? `<label>Source material (narration)</label>
      <div class="source">${esc(s.source_excerpt)}</div>` : ''}

    <label>Visual description (the shot)</label>
    <textarea id="vdesc">${esc(s.visual_description || '')}</textarea>
    <div class="row">
      <div><label>Camera move</label><select id="cmove">${moveOpts}</select></div>
      <div><label>Shot type</label><input type="text" id="stype" value="${esc(s.shot_type || '')}"></div>
    </div>
    <div class="btns">
      <button class="secondary" id="save" onclick="saveShot('${s.id}')">Save edits</button>
      <button class="secondary" id="split" onclick="splitShot('${s.id}')">Split shot</button>
    </div>
    <hr>
    <label>Prompt (sent to the image model)</label>
    <textarea id="prompt">${esc(s.prompt || '')}</textarea>
    <label>Style override (optional)</label>
    <input type="text" id="style" placeholder="leave blank to use project style">
    <button id="regen" onclick="regenerate('${s.id}')">Re-render frame</button>
    <div class="status" id="status"></div>
  `;
}

function setStatus(cls, text) {
  const st = document.getElementById('status');
  if (st) { st.className = 'status ' + cls; st.textContent = text; }
}

function applyShot(data) {
  const i = STATE.scenes.findIndex(s => s.id === data.id);
  if (i >= 0) STATE.scenes[i] = data;
  SELECTED = data.id;
  renderGrid(); renderDetail(data);
}

async function post(url, body) {
  const res = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'failed');
  return data;
}

async function saveShot(id) {
  const btn = document.getElementById('save');
  btn.disabled = true; setStatus('', 'Saving…');
  try {
    const data = await post(`/api/shots/${id}/update`, {
      visual_description: document.getElementById('vdesc').value,
      camera_move: document.getElementById('cmove').value,
      shot_type: document.getElementById('stype').value,
    });
    applyShot(data);
    setStatus('ok', '✓ Saved. Re-render to update the image.');
  } catch (e) { setStatus('err', '✗ ' + e.message); }
  const b = document.getElementById('save'); if (b) b.disabled = false;
}

async function splitShot(id) {
  const btn = document.getElementById('split');
  btn.disabled = true; setStatus('', 'Splitting…');
  try {
    const data = await post(`/api/shots/${id}/split`, { at: 0.5 });
    SELECTED = id;            // keep the first half selected
    await load();
    setStatus('ok', `✓ Split into ${esc(data.shots[0].id)} + ${esc(data.shots[1].id)}.`);
  } catch (e) { setStatus('err', '✗ ' + e.message); }
}

async function regenerate(id) {
  const btn = document.getElementById('regen');
  const prompt = document.getElementById('prompt').value.trim();
  const style = document.getElementById('style').value.trim();
  btn.disabled = true; setStatus('', 'Rendering…');
  try {
    const data = await post(`/api/scenes/${id}/regenerate`,
      { prompt: prompt || null, style: style || null });
    applyShot(data);
    setStatus('ok', '✓ Re-rendered.');
  } catch (e) {
    setStatus('err', '✗ ' + e.message);
    const b = document.getElementById('regen'); if (b) b.disabled = false;
  }
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]));
}

load();
</script>
</body>
</html>
"""
