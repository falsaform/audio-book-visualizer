import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import {
  getStateQueryKey,
  useGetJob,
  useGetState,
  useRenderAll,
  useRenderShot,
  useSplitShot,
  useUpdateShot,
} from './gen'
import type { JobOut, ShotOut, StateOut } from './gen'

const MOVES = ['', 'static', 'push_in', 'pull_out', 'pan_left', 'pan_right',
  'tilt_up', 'tilt_down', 'track_left', 'track_right']
const MOVE_LABEL: Record<string, string> = { '': '(default — Ken Burns)' }

type Status = { kind: 'ok' | 'err' | ''; msg: string }

export function App() {
  const qc = useQueryClient()
  const refreshState = () => qc.invalidateQueries({ queryKey: getStateQueryKey() })

  // Auto-poll while any render job is queued/running so images stream in.
  const state = useGetState({
    query: {
      refetchInterval: (q): number | false => {
        const d = q.state.data as StateOut | undefined
        return d && (d.jobs?.length ?? 0) > 0 ? 1200 : false
      },
    },
  })
  const renderAll = useRenderAll()
  const [selected, setSelected] = useState<string | null>(null)

  if (state.isError) {
    const msg = (state.error as Error)?.message ?? 'No production found. Run `abv visualize` first.'
    return <div className="centered">{msg}</div>
  }
  if (!state.data) return <div className="centered">Loading…</div>

  const scenes: ShotOut[] = state.data.scenes ?? []
  const shot = scenes.find((s) => s.id === selected) ?? null
  const rendered = scenes.filter((s) => s.rendered).length
  const active = state.data.jobs?.length ?? 0
  const pending = scenes.length - rendered

  const onGenerateAll = () =>
    renderAll.mutate({ data: { only_missing: true } }, { onSuccess: refreshState })

  return (
    <>
      <Header
        state={state.data} rendered={rendered} total={scenes.length} active={active}
        busy={renderAll.isPending} canGenerate={pending > 0 && active === 0}
        onGenerateAll={onGenerateAll}
      />
      <div className="layout">
        <Grid scenes={scenes} selected={selected} onSelect={setSelected} />
        {shot ? (
          <Detail key={shot.id} shot={shot} />
        ) : (
          <aside className="empty">Select a shot to view its source, prompt and image.</aside>
        )}
      </div>
    </>
  )
}

function Header({ state, rendered, total, active, busy, canGenerate, onGenerateAll }: {
  state: StateOut; rendered: number; total: number; active: number
  busy: boolean; canGenerate: boolean; onGenerateAll: () => void
}) {
  const n = state.continuity?.length ?? 0
  const label = active > 0 ? `Rendering ${active}…`
    : canGenerate ? `Generate all (${total - rendered})`
    : 'All rendered'
  return (
    <header>
      <h1>{state.title || 'Audiobook Visualizer'}</h1>
      <span className="sub">{state.author ? `— ${state.author}` : ''}</span>
      <span className="count">{rendered}/{total} shots rendered</span>
      <span className="spacer" />
      {n > 0 && (
        <span className="badge warn" title={(state.continuity ?? []).map((c) => `[${c.severity}] ${c.message}`).join('\n')}>
          ⚠ {n} continuity note{n > 1 ? 's' : ''}
        </span>
      )}
      <span className="badge">{state.provider}</span>
      <button className="gen" onClick={onGenerateAll} disabled={busy || active > 0 || !canGenerate}>
        {label}
      </button>
    </header>
  )
}

function Grid({ scenes, selected, onSelect }: {
  scenes: ShotOut[]; selected: string | null; onSelect: (id: string) => void
}) {
  let lastHeading: string | null = null
  return (
    <div className="grid">
      {scenes.flatMap((s) => {
        const nodes = []
        if (s.scene_heading && s.scene_heading !== lastHeading) {
          lastHeading = s.scene_heading
          nodes.push(<h2 key={`h-${s.id}`} className="section">{s.scene_heading}</h2>)
        }
        nodes.push(
          <div key={s.id} className={'card' + (s.id === selected ? ' selected' : '')} onClick={() => onSelect(s.id)}>
            {s.image_url ? <img loading="lazy" src={s.image_url} alt="" /> : <div className="ph">not generated</div>}
            <div className="cap">
              <div className="ch">{s.chapter || ''}</div>
              {s.title || s.id} {s.camera_move && <span className="mv">▶ {s.camera_move}</span>}
            </div>
          </div>,
        )
        return nodes
      })}
    </div>
  )
}

function Detail({ shot }: { shot: ShotOut }) {
  const qc = useQueryClient()
  const refreshState = () => qc.invalidateQueries({ queryKey: getStateQueryKey() })

  const [vdesc, setVdesc] = useState(shot.visual_description ?? '')
  const [cmove, setCmove] = useState(shot.camera_move ?? '')
  const [stype, setStype] = useState(shot.shot_type ?? '')
  const [prompt, setPrompt] = useState(shot.prompt ?? '')
  const [style, setStyle] = useState('')
  const [status, setStatus] = useState<Status>({ kind: '', msg: '' })
  const [jobId, setJobId] = useState<number | null>(null)

  const update = useUpdateShot()
  const split = useSplitShot()
  const render = useRenderShot()
  const job = useGetJob(jobId ?? 0, {
    query: {
      enabled: jobId != null,
      refetchInterval: (q): number | false => {
        const s = (q.state.data as JobOut | undefined)?.status
        return s === 'done' || s === 'error' ? false : 700
      },
    },
  })

  useEffect(() => {
    const s = job.data?.status
    if (jobId == null || !s) return
    if (s === 'done') { setStatus({ kind: 'ok', msg: '✓ Re-rendered.' }); setJobId(null); refreshState() }
    if (s === 'error') { setStatus({ kind: 'err', msg: '✗ ' + (job.data?.error || 'render failed') }); setJobId(null) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.data?.status])

  const fail = (e: unknown) => setStatus({ kind: 'err', msg: '✗ ' + (e as Error).message })

  const onSave = () => {
    setStatus({ kind: '', msg: 'Saving…' })
    update.mutate(
      { slug: shot.id, data: { visual_description: vdesc, camera_move: cmove, shot_type: stype } },
      { onSuccess: () => { refreshState(); setStatus({ kind: 'ok', msg: '✓ Saved. Re-render to update the image.' }) }, onError: fail },
    )
  }
  const onSplit = () => {
    setStatus({ kind: '', msg: 'Splitting…' })
    split.mutate({ slug: shot.id, data: { at: 0.5 } },
      { onSuccess: () => { refreshState(); setStatus({ kind: 'ok', msg: '✓ Split into two.' }) }, onError: fail })
  }
  const onRender = () => {
    setStatus({ kind: '', msg: 'Queued…' })
    render.mutate({ slug: shot.id, data: { prompt: prompt || null, style: style || null } },
      { onSuccess: (j) => setJobId(j.id), onError: fail })
  }

  const rendering = jobId != null || render.isPending

  return (
    <aside>
      {shot.image_url
        ? <img className="detail" src={shot.image_url} alt="" />
        : <div className="ph detail-ph">not generated yet</div>}
      <h2>{shot.title || shot.id}</h2>
      <div className="slug">{shot.scene_heading ? shot.scene_heading + ' · ' : ''}{shot.id}</div>
      <div className="meta">
        {shot.chapter && <div><b>Chapter:</b> {shot.chapter}</div>}
        {shot.summary && <div>{shot.summary}</div>}
        {shot.setting && <div><b>Setting:</b> {shot.setting}</div>}
        <div>{[shot.time_of_day, shot.mood].filter(Boolean).join(' · ')}</div>
        {(shot.characters_present ?? []).length > 0 && (
          <div className="chips">{(shot.characters_present ?? []).map((c) => <span key={c} className="chip">{c}</span>)}</div>
        )}
        {(shot.reference_urls ?? []).length > 0 && (
          <div><b>References:</b><div className="refs">{(shot.reference_urls ?? []).map((u) => <img key={u} src={u} alt="ref" />)}</div></div>
        )}
      </div>

      {shot.source_excerpt && (
        <>
          <label>Source material (narration)</label>
          <div className="source">{shot.source_excerpt}</div>
        </>
      )}

      <label>Visual description (the shot)</label>
      <textarea value={vdesc} onChange={(e) => setVdesc(e.target.value)} />
      <div className="row">
        <div>
          <label>Camera move</label>
          <select value={cmove} onChange={(e) => setCmove(e.target.value)}>
            {MOVES.map((m) => <option key={m} value={m}>{MOVE_LABEL[m] ?? m}</option>)}
          </select>
        </div>
        <div>
          <label>Shot type</label>
          <input type="text" value={stype} onChange={(e) => setStype(e.target.value)} />
        </div>
      </div>
      <div className="btns">
        <button className="secondary" onClick={onSave} disabled={update.isPending}>Save edits</button>
        <button className="secondary" onClick={onSplit} disabled={split.isPending}>Split shot</button>
      </div>

      <hr />
      <label>Prompt (sent to the image model)</label>
      <textarea className="prompt" value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      <label>Style override (optional)</label>
      <input type="text" value={style} placeholder="leave blank to use project style" onChange={(e) => setStyle(e.target.value)} />
      <button onClick={onRender} disabled={rendering}>{rendering ? 'Rendering…' : 'Re-render frame'}</button>
      <div className={'status ' + status.kind}>{status.msg}</div>
    </aside>
  )
}
