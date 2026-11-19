import { JSONObj } from '../lib/api'

export function Sparkline({
  points,
  width = 320,
  height = 64,
  tone = '#2563eb',
}: {
  points: number[]
  width?: number
  height?: number
  tone?: string
}) {
  if (!points.length) return <div className="note">无数据</div>
  const min = Math.min(...points)
  const max = Math.max(...points)
  const span = max - min || 1
  const step = width / Math.max(1, points.length - 1)
  const d = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${(i * step).toFixed(1)},${(height - ((p - min) / span) * (height - 6) - 3).toFixed(1)}`)
    .join(' ')
  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`} height={height} preserveAspectRatio="none">
      <path d={`${d} L${width},${height} L0,${height} Z`} fill={tone} className="area" />
      <path d={d} stroke={tone} className="line" />
    </svg>
  )
}

type Band = { kind: 'missing' | 'pending' | 'maintenance' | 'beyond' | 'verify'; from: string; to: string }

export function Curve({
  series,
  bands,
  cutoff,
  provisionalFrom,
  height = 150,
}: {
  series: { slot: string; value: number }[]
  bands: Band[]
  cutoff?: string | null
  provisionalFrom?: string | null
  height?: number
}) {
  const W = 760
  const H = height
  const padL = 42
  const padR = 10
  const padT = 10
  const padB = 20
  const times = series.map((s) => new Date(s.slot).getTime())
  const extraBands = bands
    .flatMap((b) => [new Date(b.from).getTime(), new Date(b.to).getTime()])
    .concat(cutoff ? [new Date(cutoff).getTime()] : [])
  const all = times.concat(extraBands)
  const tMin = Math.min(...all)
  const tMax = Math.max(...all)
  const values = series.map((s) => s.value)
  const vMax = Math.max(1, ...values)
  const x = (t: number) => padL + ((t - tMin) / Math.max(1, tMax - tMin)) * (W - padL - padR)
  const y = (v: number) => H - padB - (v / vMax) * (H - padT - padB)

  const d = series
    .map((s, i) => `${i === 0 ? 'M' : 'L'}${x(new Date(s.slot).getTime()).toFixed(1)},${y(s.value).toFixed(1)}`)
    .join(' ')

  const ticks = 4
  const gridY = Array.from({ length: ticks + 1 }, (_, i) => (vMax / ticks) * i)

  const bandColor: Record<Band['kind'], string> = {
    missing: '#b3261e',
    pending: '#a35a06',
    maintenance: '#7a8290',
    beyond: '#cdd3dc',
    verify: '#7a3ea8',
  }

  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} height={H} role="img">
      {gridY.map((v) => (
        <g key={v}>
          <line className="gridline" x1={padL} x2={W - padR} y1={y(v)} y2={y(v)} />
          <text x={padL - 6} y={y(v) + 3} textAnchor="end">
            {v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)}
          </text>
        </g>
      ))}
      {bands.map((b, i) => (
        <rect
          key={i}
          x={x(new Date(b.from).getTime())}
          y={padT}
          width={Math.max(1.5, x(new Date(b.to).getTime()) - x(new Date(b.from).getTime()))}
          height={H - padT - padB}
          fill={bandColor[b.kind]}
          opacity={b.kind === 'beyond' ? 0.35 : 0.18}
        />
      ))}
      <path d={`${d} L${x(new Date(series[series.length - 1].slot).getTime())},${H - padB} L${padL},${H - padB} Z`} fill="#2563eb" opacity={0.07} />
      <path d={d} className="line" stroke="#2563eb" />
      {cutoff && (
        <line
          className="cutoff"
          x1={x(new Date(cutoff).getTime())}
          x2={x(new Date(cutoff).getTime())}
          y1={padT}
          y2={H - padB}
        />
      )}
      <line className="axis" x1={padL} x2={W - padR} y1={H - padB} y2={H - padB} />
      <text x={padL} y={H - 6}>
        {new Date(tMin).toISOString().slice(11, 16)}
      </text>
      <text x={W - padR} y={H - 6} textAnchor="end">
        {new Date(tMax).toISOString().slice(11, 16)}
      </text>
      {cutoff && (
        <text x={x(new Date(cutoff).getTime()) + 3} y={padT + 9} fill="#4b515c">
          截止点
        </text>
      )}
    </svg>
  )
}

export function CoverageWaffle({ states }: { states: { status: string }[] }) {
  return (
    <div className="waffle" title={`${states.length} 个槽位`}>
      {states.map((s, i) => {
        let cls = ''
        if (s.status === 'missing') cls = 'missing'
        else if (s.status === 'pending') cls = 'pending'
        else if (s.status === 'maintenance') cls = 'maintenance'
        else if (s.status === 'beyond_cutoff') cls = 'beyond'
        else if (s.status === 'verification') cls = 'verify'
        return <i key={i} className={cls} />
      })}
    </div>
  )
}

export function BarMeter({ value, tone }: { value: number; tone?: 'good' | 'warn' | 'bad' | 'info' }) {
  const pct = Math.max(0, Math.min(1, value))
  const cls = tone && tone !== 'good' ? tone : pct >= 0.999 ? 'good' : 'warn'
  return (
    <div className={`bar ${cls}`}>
      <i style={{ width: `${pct * 100}%` }} />
    </div>
  )
}

export function RangeChips({ ranges }: { ranges: JSONObj[] }) {
  if (!ranges?.length) return <span className="note">无</span>
  return (
    <div className="chips">
      {ranges.map((r, i) => (
        <span key={i} className="tag">
          {r.from?.slice(11, 16)}–{r.to?.slice(11, 16)}
          {r.slots ? ` · ${r.slots.length}槽` : ''}
        </span>
      ))}
    </div>
  )
}
