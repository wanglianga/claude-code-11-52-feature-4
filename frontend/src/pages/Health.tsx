import { useEffect, useMemo, useState } from 'react'
import { api, fmtTime, fmtWindow, JSONObj, STATUS_LABEL } from '../lib/api'
import { useApp } from '../lib/store'
import { PageHead, StatusPill } from '../components/ui'
import { BarMeter, CoverageWaffle, Curve, RangeChips } from '../components/charts'

export default function HealthPage() {
  const { meta, refreshKey, run, toast } = useApp()
  const [evals, setEvals] = useState<JSONObj[]>([])
  const [products, setProducts] = useState<JSONObj[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<JSONObj | null>(null)
  const [meterFocus, setMeterFocus] = useState<string | null>(null)
  const [windowFilter, setWindowFilter] = useState<string>('')

  const windows = (meta?.clock?.windows as string[]) ?? []

  useEffect(() => {
    if (windows.length && !windowFilter) setWindowFilter(windows[windows.length - 1])
  }, [windows, windowFilter])

  useEffect(() => {
    const load = async () => {
      const [e, p] = await Promise.all([
        api.get<JSONObj[]>('/api/health/evaluations'),
        api.get<JSONObj>('/api/products'),
      ])
      setEvals(e)
      setProducts(p.runs ?? [])
    }
    load().catch((err) => toast(String(err), 'bad'))
  }, [refreshKey, toast])

  const filtered = useMemo(
    () => evals.filter((e) => !windowFilter || e.window_start === windowFilter),
    [evals, windowFilter],
  )

  useEffect(() => {
    if (!filtered.length) return
    const still = filtered.find((e) => e.id === selected)
    if (!still) setSelected(filtered[0].id)
  }, [filtered, selected])

  useEffect(() => {
    if (!selected) return
    api
      .get<JSONObj>(`/api/health/evaluations/${selected}`)
      .then(setDetail)
      .catch((err) => toast(String(err), 'bad'))
  }, [selected, toast])

  const selectedEval = filtered.find((e) => e.id === selected)
  const meter = detail?.details?.meters?.find((m: JSONObj) => m.meter_code === meterFocus) ?? null
  const relatedProducts = products.filter(
    (p) =>
      selectedEval &&
      p.product_field === selectedEval.product_field &&
      p.window_start === selectedEval.window_start,
  )

  return (
    <>
      <PageHead
        title="产品健康"
        lead="每个观察窗口由完整重算得出：预期槽位由采样承诺推导，维护只扣除规则允许的时段，重复样本只占一个槽；健康状态 = 新鲜度 + 覆盖 + 核验条件，与采集任务存活、事故是否关闭无关。"
        actions={
          <>
            <div className="seg">
              {windows.map((w) => (
                <button key={w} className={w === windowFilter ? 'on' : ''} onClick={() => setWindowFilter(w)}>
                  {fmtTime(w)}
                </button>
              ))}
            </div>
            <button
              className="btn"
              onClick={async () => {
                const res = await run(api.post('/api/health/reevaluate', {}), '已完成窗口重算')
                if (res) window.location.reload()
              }}
            >
              重算当前日
            </button>
          </>
        }
      />

      <div className="split-2">
        <div className="panel">
          <div className="panel-head">
            <h2>观察窗口 · 数据来源</h2>
            <span className="hint">
              {windowFilter ? fmtWindow(windowFilter, addWindow(windowFilter)) : '选择窗口'}
            </span>
          </div>
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr>
                  <th>来源</th>
                  <th>状态</th>
                  <th>覆盖（分子/分母）</th>
                  <th>新鲜度</th>
                  <th>核验</th>
                  <th>版本</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((e) => (
                  <tr key={e.id} className={`click ${e.id === selected ? 'selected' : ''}`} onClick={() => setSelected(e.id)}>
                    <td>
                      <div style={{ fontWeight: 560 }}>{e.source_name}</div>
                      <div className="note">{e.product_field}</div>
                    </td>
                    <td>
                      <StatusPill status={e.status} />
                    </td>
                    <td style={{ minWidth: 150 }}>
                      <div className="mono num">
                        {e.coverage_num} / {e.coverage_den}
                        <span className="note"> · {e.coverage != null ? `${(e.coverage * 100).toFixed(1)}%` : '—'}</span>
                      </div>
                      <BarMeter value={e.coverage ?? 0} />
                    </td>
                    <td>
                      <span className={`pill ${e.freshness === 'current' ? 'good' : e.freshness === 'stale' ? 'bad' : 'warn'}`}>
                        {STATUS_LABEL[e.freshness] ?? e.freshness}
                      </span>
                    </td>
                    <td>{e.verification_ok ? <span className="pill good">通过</span> : <span className="pill bad">缺口</span>}</td>
                    <td className="num">r{e.revision} · 重算 {e.recompute_count}</td>
                  </tr>
                ))}
                {!filtered.length && (
                  <tr>
                    <td colSpan={6} className="empty">
                      该窗口尚无已完成评估
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>合格判定</h2>
            {selectedEval && <span className="hint">评估 #{selectedEval.id}</span>}
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 10 }}>
            {selectedEval ? (
              <>
                <div className="row wrap">
                  <StatusPill status={selectedEval.status} />
                  <span className={`pill ${selectedEval.certified ? 'good' : 'warn'}`}>
                    {selectedEval.certified ? '已核验' : selectedEval.confirmable ? '可确认未核验' : '未达可确认'}
                  </span>
                  <span className="tag">截止点 {fmtTime(selectedEval.data_cutoff)}</span>
                </div>
                <dl className="kv">
                  <dt>缺失槽位</dt>
                  <dd>{selectedEval.counts?.missing ?? 0}</dd>
                  <dt>待定槽位</dt>
                  <dd>{selectedEval.counts?.pending ?? 0}</dd>
                  <dt>迟到槽位</dt>
                  <dd>{selectedEval.counts?.late ?? 0}</dd>
                  <dt>维护扣除</dt>
                  <dd>{selectedEval.counts?.maintenance ?? 0}</dd>
                  <dt>核验缺口</dt>
                  <dd>{selectedEval.counts?.verification ?? 0}</dd>
                  <dt>重复到达</dt>
                  <dd>{selectedEval.counts?.duplicates ?? 0}</dd>
                  <dt>声明零用量</dt>
                  <dd>{selectedEval.counts?.declared_zero ?? 0}</dd>
                  <dt>未到截止槽位</dt>
                  <dd>{selectedEval.counts?.beyond_cutoff ?? 0}</dd>
                </dl>
                <div className="note">
                  健康与合格只由覆盖、核验和截止点决定；采集任务存活、人工关闭事故都不参与判定。
                </div>
              </>
            ) : (
              <div className="empty">选择左侧任一评估</div>
            )}
          </div>
        </div>
      </div>

      {detail && (
        <>
          <div className="split-2" style={{ marginTop: 14 }}>
            <div className="panel">
              <div className="panel-head">
                <h2>近实时曲线（带截止点）</h2>
                <span className="hint">
                  {relatedProducts.find((p) => p.product_code === 'energy.realtime')
                    ? `版本 v${relatedProducts.find((p) => p.product_code === 'energy.realtime')?.version}`
                    : '暂定'}
                </span>
              </div>
              <div className="panel-body">
                <CurveView evalId={detail.id} />
                <div className="legend" style={{ marginTop: 8 }}>
                  <span>
                    <i style={{ background: '#b3261e', opacity: 0.4 }} /> 缺失
                  </span>
                  <span>
                    <i style={{ background: '#a35a06', opacity: 0.4 }} /> 待定
                  </span>
                  <span>
                    <i style={{ background: '#7a8290', opacity: 0.4 }} /> 维护
                  </span>
                  <span>
                    <i style={{ background: '#cdd3dc' }} /> 未到截止
                  </span>
                </div>
              </div>
            </div>
            <div className="panel">
              <div className="panel-head">
                <h2>产品引用</h2>
                <span className="hint">同窗口运行版本</span>
              </div>
              <div className="scroll-x">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>产品</th>
                      <th>状态</th>
                      <th>版本</th>
                      <th>合格范围</th>
                    </tr>
                  </thead>
                  <tbody>
                    {relatedProducts.map((p) => (
                      <tr key={p.id}>
                        <td>{p.product_code === 'energy.realtime' ? '近实时曲线' : '核验日汇总'}</td>
                        <td>
                          <StatusPill status={p.status} />
                        </td>
                        <td className="num">v{p.version}</td>
                        <td>
                          <RangeChips ranges={p.qualified_ranges?.filter((r: JSONObj) => r.kind === 'qualified')} />
                        </td>
                      </tr>
                    ))}
                    {!relatedProducts.length && (
                      <tr>
                        <td colSpan={4} className="empty">
                          尚无产品运行
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <div className="panel" style={{ marginTop: 14 }}>
            <div className="panel-head">
              <h2>表计下钻 · 分母推导</h2>
              <span className="hint">点击表计查看槽位明细</span>
            </div>
            <div className="scroll-x">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>表计</th>
                    <th>楼栋</th>
                    <th>口径</th>
                    <th>预期/覆盖</th>
                    <th>分段（频率变化）</th>
                    <th>维护</th>
                    <th>核验</th>
                    <th>槽位</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.details.meters.map((m: JSONObj) => (
                    <tr
                      key={m.meter_id}
                      className={`click ${meterFocus === m.meter_code ? 'selected' : ''}`}
                      onClick={() => setMeterFocus(m.meter_code === meterFocus ? null : m.meter_code)}
                    >
                      <td>
                        <div style={{ fontWeight: 540 }}>{m.meter_code}</div>
                        <div className="note">{m.meter_name}</div>
                      </td>
                      <td>
                        {m.building_code} · {m.building_name}
                      </td>
                      <td className="note">{m.cumulative ? '累计表码' : '区间用量'}</td>
                      <td className="num">
                        {m.observed}/{m.expected}
                      </td>
                      <td className="note">
                        {m.segments
                          .map((s: JSONObj) => `${s.cadence_minutes}min×${s.denominator}`)
                          .join(' / ') || '—'}
                      </td>
                      <td className="num">{m.maintenance.length}</td>
                      <td>
                        {m.verification.length ? (
                          <span className="pill bad">{m.verification.length} 项</span>
                        ) : (
                          <span className="pill good">通过</span>
                        )}
                      </td>
                      <td>
                        <button
                          className="btn sm"
                          onClick={(ev) => {
                            ev.stopPropagation()
                            setMeterFocus(m.meter_code)
                          }}
                        >
                          查看
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {meter && <MeterDetail meter={meter} source={detail} />}
        </>
      )}
    </>
  )
}

function addWindow(iso: string): string {
  return new Date(new Date(iso).getTime() + 6 * 3600 * 1000).toISOString()
}

function CurveView({ evalId }: { evalId: number }) {
  const [data, setData] = useState<JSONObj | null>(null)
  useEffect(() => {
    api.get<JSONObj>(`/api/health/evaluations/${evalId}`).then((d) => {
      const meters = d.details?.meters ?? []
      const bySlot: Record<string, number> = {}
      const bands: JSONObj[] = []
      for (const m of meters) {
        const values = m.consumption?.by_slot ?? {}
        for (const [slot, v] of Object.entries(values)) {
          if (typeof v === 'number') bySlot[slot] = (bySlot[slot] ?? 0) + v
        }
        for (const s of m.slot_states ?? []) {
          if (s.status === 'covered') continue
          const kind =
            s.status === 'verification'
              ? 'verify'
              : s.status === 'beyond_cutoff'
                ? 'beyond'
                : s.status
          const end = s.slot_end || new Date(new Date(s.slot).getTime() + 15 * 60000).toISOString()
          bands.push({ kind, from: s.slot, to: end })
        }
      }
      const series = Object.entries(bySlot)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([slot, value]) => ({ slot, value }))
      setData({ series, bands, cutoff: d.data_cutoff, provisional: d.details?.counts?.beyond_cutoff })
    })
  }, [evalId])
  if (!data) return <div className="note">加载曲线…</div>
  return (
    <>
      <Curve series={data.series} bands={data.bands} cutoff={data.cutoff} />
      {data.provisional > 0 && (
        <div className="banner info" style={{ marginTop: 8 }}>
          <span>截止点之后还有 {data.provisional} 个槽位尚未到期：曲线带暂定标记，不填零。</span>
        </div>
      )}
    </>
  )
}

function MeterDetail({ meter, source }: { meter: JSONObj; source: JSONObj }) {
  const verification = meter.verification ?? []
  const segments = meter.segments ?? []
  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="panel-head">
        <h2>
          槽位明细 · {meter.meter_code}{' '}
          <span className="tag">
            {meter.observed}/{meter.expected}
          </span>
        </h2>
        <span className="hint">
          {meter.building_code} · {source.source_name} · 单位 {meter.unit}
        </span>
      </div>
      <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
        <div>
          <div className="note" style={{ marginBottom: 6 }}>
            分母分段：{segments.map((s: JSONObj) => `${s.start?.slice(11, 16)}–${s.end?.slice(11, 16)} 按 ${s.cadence_minutes} 分钟，分母 ${s.denominator}`).join('；')}
          </div>
          <CoverageWaffle states={meter.slot_states ?? []} />
          <div className="legend" style={{ marginTop: 8 }}>
            <span><i style={{ background: '#0f7a55' }} /> 已覆盖</span>
            <span><i style={{ background: '#b3261e' }} /> 缺失</span>
            <span><i style={{ background: '#a35a06' }} /> 待定</span>
            <span><i style={{ background: '#cdd3dc' }} /> 维护扣除</span>
            <span><i style={{ background: '#e8ebf0' }} /> 未到截止</span>
          </div>
        </div>
        {verification.length > 0 && (
          <div>
            <h3 style={{ marginBottom: 6 }}>核验缺口（不计为节能）</h3>
            <table className="tbl">
              <thead>
                <tr>
                  <th>槽位</th>
                  <th>类型</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {verification.map((v: JSONObj, i: number) => (
                  <tr key={i}>
                    <td className="mono">{v.slot?.slice(11, 16)}</td>
                    <td>
                      <span className="pill bad">{v.kind}</span>
                    </td>
                    <td className="note">{v.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <details className="raw">
          <summary>查看原始槽位与用量 JSON</summary>
          <pre>{JSON.stringify({ segments, slot_states: meter.slot_states, consumption: meter.consumption }, null, 2)}</pre>
        </details>
      </div>
    </div>
  )
}
