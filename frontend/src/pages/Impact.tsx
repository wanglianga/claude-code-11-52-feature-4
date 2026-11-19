import { useEffect, useState } from 'react'
import { api, fmtTime, fmtWindow, JSONObj } from '../lib/api'
import { useApp } from '../lib/store'
import { PageHead, StatusPill } from '../components/ui'
import { RangeChips } from '../components/charts'

const KIND_LABEL: Record<string, string> = {
  property_dashboard: '物业调度',
  settlement: '月度结算',
  assessment: '节能考核',
}

export default function ImpactPage() {
  const { meta, refreshKey, toast, run } = useApp()
  const [consumers, setConsumers] = useState<JSONObj[]>([])
  const [active, setActive] = useState<string>('')
  const [detail, setDetail] = useState<JSONObj | null>(null)
  const [preview, setPreview] = useState<JSONObj | null>(null)
  const [tolerance, setTolerance] = useState<number | null>(null)
  const [minCoverage, setMinCoverage] = useState<number | null>(null)

  useEffect(() => {
    api
      .get<JSONObj[]>('/api/consumers')
      .then((data) => {
        setConsumers(data)
        if (!active && data.length) setActive(data[0].code)
      })
      .catch((err) => toast(String(err), 'bad'))
  }, [refreshKey, toast, active])

  useEffect(() => {
    if (!active) return
    api
      .get<JSONObj>(`/api/consumers/${active}`)
      .then((d) => {
        setDetail(d)
        const last = d.windows?.[d.windows.length - 1]
        if (last) {
          setTolerance(last.thresholds?.tolerance_minutes ?? null)
          setMinCoverage(last.thresholds?.min_coverage ?? null)
        }
        setPreview(null)
      })
      .catch((err) => toast(String(err), 'bad'))
  }, [active, refreshKey, toast])

  const consumer = consumers.find((c) => c.code === active)
  const windows = (detail?.windows ?? []) as JSONObj[]
  const latest = windows[windows.length - 1]
  const sensitivity = windows.map((w) => ({ window: w.window_start, status: w.status, affected: w.affected_slots }))

  const doPreview = async () => {
    if (!latest) return
    const res = await run(
      api.post<JSONObj>(`/api/consumers/${active}/preview`, {
        window_start: latest.window_start,
        tolerance_minutes: tolerance,
        min_coverage: minCoverage,
      }),
    )
    if (res) setPreview(res)
  }

  return (
    <>
      <PageHead
        title="消费影响地图"
        lead="消费目录按具体产品字段、楼栋和时段登记依赖。同一产品版本上的不同消费者使用各自的容忍范围与核验要求分别判定；多个受影响产品导致的同一缺口只计一次。"
        actions={
          <span className="pill info">
            <span className="pd" />
            原始质量事实与判定分离
          </span>
        }
      />

      <div className="split-3">
        {consumers.map((c) => {
          const last = c.windows?.[c.windows.length - 1]
          const tone = last?.status ?? 'waiting'
          return (
            <div
              key={c.code}
              className="panel"
              style={{ cursor: 'pointer', outline: c.code === active ? '2px solid #bfd2fb' : 'none' }}
              onClick={() => setActive(c.code)}
            >
              <div className="panel-body" style={{ display: 'grid', gap: 8 }}>
                <div className="row">
                  <h3>{c.name}</h3>
                  <span className="spacer" />
                  <StatusPill status={tone} />
                </div>
                <div className="note">{c.description}</div>
                <dl className="kv">
                  <dt>容忍</dt>
                  <dd>{c.thresholds.tolerance_minutes} 分钟</dd>
                  <dt>最低覆盖</dt>
                  <dd>{(c.thresholds.min_coverage * 100).toFixed(0)}%</dd>
                  <dt>最大连续缺口</dt>
                  <dd>{c.thresholds.max_gap_slots} 槽</dd>
                  <dt>要求核验</dt>
                  <dd>{c.thresholds.require_certified ? '是' : '否'}</dd>
                  <dt>依赖</dt>
                  <dd>{c.dependencies.length} 项</dd>
                </dl>
                <div className="note">
                  {last
                    ? `最新窗口受影响 ${last.affected_slots} 槽 · 覆盖 ${last.coverage != null ? (last.coverage * 100).toFixed(1) + '%' : '—'}`
                    : '尚无已完成窗口判定'}
                </div>
              </div>
            </div>
          )
        })}
      </div>

      {detail && (
        <div className="split-2" style={{ marginTop: 14 }}>
          <div className="panel">
            <div className="panel-head">
              <h2>{detail.name} · 窗口判定</h2>
              <span className="hint">{KIND_LABEL[detail.kind] ?? detail.kind}</span>
            </div>
            <div className="scroll-x">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>窗口</th>
                    <th>判定</th>
                    <th>影响槽位</th>
                    <th>覆盖</th>
                    <th>受影响楼栋</th>
                    <th>区间</th>
                  </tr>
                </thead>
                <tbody>
                  {windows.map((w) => (
                    <tr key={w.id}>
                      <td className="mono">{fmtWindow(w.window_start, w.window_end)}</td>
                      <td>
                        <StatusPill status={w.status} />
                      </td>
                      <td className="num">
                        {w.affected_slots} / {w.den ?? '—'}
                      </td>
                      <td className="num">{w.coverage != null ? `${(w.coverage * 100).toFixed(1)}%` : '—'}</td>
                      <td>{w.affected_buildings?.length ? w.affected_buildings.join('、') : '—'}</td>
                      <td>
                        <RangeChips ranges={w.ranges} />
                      </td>
                    </tr>
                  ))}
                  {!windows.length && (
                    <tr>
                      <td colSpan={6} className="empty">
                        尚无判定
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2>依赖分解</h2>
              <span className="hint">每个依赖字段/楼栋分别计算</span>
            </div>
            <div className="scroll-x">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>产品</th>
                    <th>字段</th>
                    <th>楼栋</th>
                    <th>判定</th>
                    <th>影响槽位</th>
                  </tr>
                </thead>
                <tbody>
                  {(latest?.dependencies ?? []).map((d: JSONObj, i: number) => (
                    <tr key={i}>
                      <td className="note">{d.product_code === 'energy.daily' ? '核验日汇总' : '近实时曲线'}</td>
                      <td>{d.product_field}</td>
                      <td>{d.building_code ?? '全部'}</td>
                      <td>
                        <StatusPill status={d.status} />
                      </td>
                      <td className="num">{d.affected_slots}</td>
                    </tr>
                  ))}
                  {!latest?.dependencies?.length && (
                    <tr>
                      <td colSpan={5} className="empty">
                        无依赖登记
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            {latest && (
              <div className="panel-body" style={{ borderTop: '1px solid var(--line)' }}>
                <div className="note" style={{ marginBottom: 6 }}>
                  按楼栋的结果（缺失表计不牵连无关楼栋）：
                </div>
                <div className="chips">
                  {(latest.dependencies ?? [])
                    .flatMap((d: JSONObj) => (d.per_building ?? []).map((b: JSONObj) => ({ ...b, dep: d })))
                    .map((b: JSONObj, i: number) => (
                      <span key={i} className={`pill ${b.status === 'ok' ? 'good' : b.status === 'restricted' ? 'warn' : 'bad'}`}>
                        {b.building} · {b.affected}/{b.den}
                      </span>
                    ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {latest && (
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="panel-head">
            <h2>阈值敏感性</h2>
            <span className="hint">改变容忍范围只改变判定，不改变原始质量事实</span>
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
            <div className="row wrap">
              <label className="field">
                容忍（分钟）
                <input
                  className="input"
                  type="number"
                  value={tolerance ?? ''}
                  onChange={(e) => setTolerance(Number(e.target.value))}
                />
              </label>
              <label className="field">
                最低覆盖 (0–1)
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  value={minCoverage ?? ''}
                  onChange={(e) => setMinCoverage(Number(e.target.value))}
                />
              </label>
              <button className="btn primary" style={{ alignSelf: 'flex-end' }} onClick={doPreview}>
                预览判定变化
              </button>
              <div className="spacer" />
              <span className="tag">窗口 {fmtWindow(latest.window_start, latest.window_end)}</span>
            </div>
            {preview && (
              <>
                <div className="diff">
                  <div className="panel" style={{ boxShadow: 'none' }}>
                    <div className="panel-body" style={{ display: 'grid', gap: 6 }}>
                      <div className="note">当前阈值</div>
                      <div className="row">
                        <StatusPill status={preview.current.status} />
                        <span className="num">{preview.current.affected_slots} 槽受影响</span>
                      </div>
                    </div>
                  </div>
                  <div className="arrow">→</div>
                  <div className="panel" style={{ boxShadow: 'none' }}>
                    <div className="panel-body" style={{ display: 'grid', gap: 6 }}>
                      <div className="note">预览阈值</div>
                      <div className="row">
                        <StatusPill status={preview.preview.status} />
                        <span className="num">{preview.preview.affected_slots} 槽受影响</span>
                      </div>
                    </div>
                  </div>
                </div>
                {preview.changed_dependencies?.length > 0 ? (
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th>依赖</th>
                        <th>前</th>
                        <th>后</th>
                        <th>影响槽位</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.changed_dependencies.map((c: JSONObj, i: number) => (
                        <tr key={i}>
                          <td>
                            {c.product_field} · {c.building_code ?? '全部'}
                          </td>
                          <td>
                            <StatusPill status={c.before} />
                          </td>
                          <td>
                            <StatusPill status={c.after} />
                          </td>
                          <td className="num">
                            {c.affected_slots_before} → {c.affected_slots_after}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="banner info">
                    <span>阈值变化不影响当前窗口判定；原始质量事实（缺失/迟到/核验缺口）保持不变。</span>
                  </div>
                )}
              </>
            )}
            <div className="note">
              已读取的历史判定：
              {sensitivity.map((s, i) => (
                <span key={i} className="tag" style={{ marginLeft: 6 }}>
                  {fmtTime(s.window)} {s.status}/{s.affected}
                </span>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
