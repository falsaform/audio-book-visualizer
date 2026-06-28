"""The single-page web UI (self-contained HTML + CSS + JS).

Kept as one string so the server has no template/static build step. It talks to
the JSON API in ``server.py``: ``GET /api/state`` and
``POST /api/scenes/{id}/regenerate``.
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
  header .badge { margin-left: auto; background: #1f2d3d; color: #7ad; padding: .25rem .6rem;
                  border-radius: 999px; font-size: .8rem; }
  .layout { display: grid; grid-template-columns: 1fr 420px; gap: 0; height: calc(100vh - 58px); }
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
  aside { border-left: 1px solid #333; overflow-y: auto; padding: 1.25rem; background: #151515; }
  aside.empty { display: flex; align-items: center; justify-content: center; color: #666; text-align: center; }
  aside img.detail { width: 100%; border-radius: 8px; aspect-ratio: 16/9; object-fit: cover; background: #222; }
  aside h2 { font-size: 1.05rem; margin: .8rem 0 .3rem; }
  .meta { font-size: .85rem; color: #bbb; line-height: 1.5; }
  .meta b { color: #ddd; }
  .chips { display: flex; flex-wrap: wrap; gap: .35rem; margin: .4rem 0; }
  .chip { background: #233; color: #9cd; padding: .15rem .5rem; border-radius: 6px; font-size: .75rem; }
  .refs { display: flex; gap: .4rem; margin: .4rem 0; }
  .refs img { width: 48px; height: 48px; object-fit: cover; border-radius: 6px; border: 1px solid #345; }
  label { display: block; font-size: .78rem; color: #9aa; margin: .8rem 0 .25rem; text-transform: uppercase; letter-spacing: .04em; }
  textarea, input[type=text] { width: 100%; background: #0e0e0e; color: #eee; border: 1px solid #333;
            border-radius: 6px; padding: .55rem; font-family: inherit; font-size: .85rem; }
  textarea { min-height: 130px; resize: vertical; }
  button { margin-top: .9rem; width: 100%; background: #2563eb; color: #fff; border: 0;
           border-radius: 7px; padding: .6rem; font-size: .95rem; cursor: pointer; }
  button:disabled { opacity: .6; cursor: progress; }
  .status { margin-top: .6rem; font-size: .82rem; min-height: 1.1em; }
  .status.err { color: #e88; }
  .status.ok { color: #8d8; }
</style>
</head>
<body>
<header>
  <h1 id="title">Audiobook Visualizer</h1>
  <span class="sub" id="sub"></span>
  <span class="badge" id="provider"></span>
</header>
<div class="layout">
  <div class="grid" id="grid"></div>
  <aside id="detail" class="empty">Select a frame to view and regenerate it.</aside>
</div>

<script>
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
  renderGrid();
  if (SELECTED) renderDetail(STATE.scenes.find(s => s.id === SELECTED));
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
    card.innerHTML = media +
      `<div class="cap"><div class="ch">${esc(s.chapter || '')}</div>${esc(s.title || s.id)}</div>`;
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
  d.innerHTML = `
    ${media}
    <h2>${esc(s.title || s.id)}</h2>
    <div class="meta">
      ${s.chapter ? `<div><b>Chapter:</b> ${esc(s.chapter)}</div>` : ''}
      ${s.summary ? `<div>${esc(s.summary)}</div>` : ''}
      ${s.setting ? `<div><b>Setting:</b> ${esc(s.setting)}</div>` : ''}
      ${[s.time_of_day, s.mood, s.shot_type].filter(Boolean).map(esc).join(' · ')}
      ${chips ? `<div class="chips">${chips}</div>` : ''}
      ${refs ? `<div><b>References:</b><div class="refs">${refs}</div></div>` : ''}
    </div>
    <label>Prompt (edit to steer the image)</label>
    <textarea id="prompt">${esc((s.frame && s.frame.prompt) || '')}</textarea>
    <label>Style override (optional)</label>
    <input type="text" id="style" placeholder="leave blank to use project style">
    <button id="regen" onclick="regenerate('${s.id}')">Regenerate frame</button>
    <div class="status" id="status"></div>
  `;
}

async function regenerate(id) {
  const btn = document.getElementById('regen');
  const status = document.getElementById('status');
  const prompt = document.getElementById('prompt').value.trim();
  const style = document.getElementById('style').value.trim();
  btn.disabled = true; status.className = 'status'; status.textContent = 'Generating…';
  try {
    const res = await fetch(`/api/scenes/${id}/regenerate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: prompt || null, style: style || null }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'failed');
    const i = STATE.scenes.findIndex(s => s.id === id);
    STATE.scenes[i] = data;
    renderGrid(); renderDetail(data);
    const s2 = document.getElementById('status');
    s2.className = 'status ok'; s2.textContent = '✓ Regenerated.';
  } catch (e) {
    status.className = 'status err'; status.textContent = '✗ ' + e.message;
    btn.disabled = false;
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
