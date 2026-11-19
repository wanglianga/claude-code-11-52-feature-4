import { useEffect, useMemo, useState } from 'react'
import { api, fmtTime, fmtWindow, JSONObj } from '../lib/api'
import { useApp } from '../lib/store'
import { PageHead, StatusPill } from '../components/ui'
import { Curve } from '../components/charts'

const STATUS_TEXT: Record<string, string> = {
  qualified: '合格',
  provisional: '暂定',
  partial: '部分可用',
  failed: '不合格',
  pending: '待定',
  unavailable: '不可用',
}

export default function ProductsPage() {
  const { meta, refreshKey, toast } = useApp()
  const [runs, setRuns] = useState<JSONObj[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<JSONObj | null>(null)
  const windows = (meta?.clock?.windows as string[]) ?? []
  const [windowFilter, setWindowFilter] = useState('')

  useEffect(() => {
    if (windows.length && !windowFilter) setWindowFilter(windows[windows.length - 1])
  }, [windows, windowFilter])

  useEffect(() => {
    api
      .get<JSONObj>('/api/products')
      .then((d) => {
        setRuns(d.runs ?? [])
        if (!selected && d.runs?.length) setSelected(d.runs[0].id)
      })
      .catch((err) => toast(String(err), 'bad'))
  }, [refreshKey, toast, selected])

  useEffect(() => {
    if (!selected) return
    api.get<JSONObj>(`/api/products/${selected}`).then(setDetail).catch((err) => toast(String(err), 'bad'))
  }, [selected, toast])

  const filtered = useMemo(
    () => runs.filter((r) => !windowFilter || r.window_start === windowFilter),
    [runs, windowFilter],
  )

  const series = ((detail?.output?.series ?? []) as JSONObj[]) as { slot: string; value: number }[]
  const ranges = (detail?.qualified_ranges ?? []) as JSONObj[]
  const bands = ranges
    .filter((r) => r.kind !== 'qualified')
    .map((r) => ({ kind: 'maintenance' as const, from: r.from, to: r.to }))
  for (const r of ranges.filter((x) => x.kind === 'qualified')) {
    // qualified segments are drawn implicitly by a continuous line
    void r
  }
  const cutoff = detail?.data_cutoff
  const provisionalFrom = detail?.output?.provisional_from

  return (
    <>
      <PageHead
        title="产品运行"
        lead="近实时曲线与核验日汇总分别保存输出与合格范围。近实时曲线按截止点输出暂定状态；日汇总只有窗口完整重算、覆盖全部到期槽位并通过核验后才是合格版本。"
        actions={
          <div className="seg">
            {windows.map((w) => (
              <button key={w} className={w === windowFilter ? 'on' : ''} onClick={() => setWindowFilter(w)}>
                {fmtTime(w)}
              </button>
            ))}
          </div>
        }
      />

      <div className="split-2">
        <div className="panel">
          <div className="panel-head">
            <h2>运行版本</h2>
            <span className="hint">{windowFilter ? fmtWindow(windowFilter, addWindow(windowFilter)) : ''}</span>
          </div>
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr>
                  <th>产品</th>
                  <th>来源</th>
                  <th>状态</th>
                  <th>版本</th>
                  <th>合计</th>
                  <th>截止点</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <tr key={r.id} className={`click ${r.id === selected ? 'selected' : ''}`} onClick={() => setSelected(r.id)}>
                    <td>
                      <div style={{ fontWeight: 540 }}>{productName(r.product_code)}</div>
                      <div className="note">{r.product_code}</div>
                    </td>
                    <td>{r.source_name}</td>
                    <td>
                      <StatusPill status={r.status} label={STATUS_TEXT[r.status]} />
                    </td>
                    <td className="num">v{r.version}</td>
                    <td className="num">
                      {r.total ?? '—'} <span className="note">{r.unit}</span>
                    </td>
                    <td className="mono">{fmtTime(r.data_cutoff)}</td>
                  </tr>
                ))}
                {!filtered.length && (
                  <tr>
                    <td colSpan={6} className="empty">
                      当前窗口没有产品运行
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>版本谱系</h2>
            <span className="hint">仅列出已生成版本</span>
          </div>
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr>
                  <th>版本</th>
                  <th>状态</th>
                  <th>截止点</th>
                  <th>合计</th>
                </tr>
              </thead>
              <tbody>
                {(detail?.versions ?? [])
                  .slice()
                  .reverse()
                  .map((v: JSONObj) => (
                    <tr key={v.id}>
                      <td className="num">v{v.version}·r{v.revision}</td>
                      <td>
                        <StatusPill status={v.status} label={STATUS_TEXT[v.status]} />
                      </td>
                      <td className="mono">{fmtTime(v.data_cutoff)}</td>
                      <td className="num">{v.total ?? '—'}</td>
                    </tr>
                  ))}
                {!detail?.versions?.length && (
                  <tr>
                    <td colSpan={4} className="empty">
                      选择左侧产品查看版本
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {detail && (
        <>
          <div className="panel" style={{ marginTop: 14 }}>
            <div className="panel-head">
              <h2>
                {productName(detail.product_code)} · {detail.source_name}{' '}
                <span className="tag">
                  {detail.window_start?.slice(11, 16)}–{detail.window_end?.slice(11, 16)}
                </span>
              </h2>
              <div className="row">
                <StatusPill status={detail.status} label={STATUS_TEXT[detail.status]} />
                <span className="note">截止点 {fmtTime(detail.data_cutoff)}</span>
              </div>
            </div>
            <div className="panel-body">
              <Curve
                series={series}
                bands={bands}
                cutoff={cutoff}
                provisionalFrom={provisionalFrom}
                height={170}
              />
              <div className="row wrap" style={{ marginTop: 8 }}>
                <span className="tag">合计 {detail.output?.total ?? 0} {detail.output?.unit}</span>
                <span className="tag">
                  序列 {series.length} 槽
                </span>
                {provisionalFrom && <span className="pill warn">暂定自 {fmtTime(provisionalFrom)}</span>}
                <span className="spacer" />
                <span className="note">合格范围（绿色区间为完整合格段）</span>
              </div>
              <div className="chips" style={{ marginTop: 8 }}>
                {ranges.map((r, i) => (
                  <span key={i} className={`tag`}>
                    {r.kind === 'qualified' ? '合格 ' : '维护 '}
                    {r.from?.slice(11, 16)}–{r.to?.slice(11, 16)}
                  </span>
                ))}
              </div>
            </div>
          </div>

          <div className="panel" style={{ marginTop: 14 }}>
            <div className="panel-head">
              <h2>消费者引用</h2>
              <span className="hint">此产品版本被哪些消费者依赖、其判定如何</span>
            </div>
            <div className="scroll-x">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>消费者</th>
                    <th>类型</th>
                    <th>楼栋</th>
                    <th>判定</th>
                    <th>容忍</th>
                    <th>要求核验</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.consumers ?? []).map((c: JSONObj, i: number) => (
                    <tr key={i}>
                      <td>{c.consumer_name}</td>
                      <td className="note">{kindLabel(c.kind)}</td>
                      <td>{c.building_code ?? '全部'}</td>
                      <td>
                        <StatusPill status={c.impact_status} />
                      </td>
                      <td className="num">{c.tolerance_minutes} 分钟</td>
                      <td>{c.require_certified ? <span className="pill bad">是</span> : <span className="pill muted">否</span>}</td>
                    </tr>
                  ))}
                  {!detail.consumers?.length && (
                    <tr>
                      <td colSpan={6} className="empty">
                        无消费者引用
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  )
}

function productName(code: string) {
  return code === 'energy.realtime' ? '近实时能耗曲线' : '核验日汇总'
}

function kindLabel(kind: string) {
  if (kind === 'property_dashboard') return '物业调度'
  if (kind === 'settlement') return '月度结算'
  if (kind === 'assessment') return '节能考核'
  return kind
}

function addWindow(iso: string) {
  return new Date(new Date(iso).getTime() + 6 * 3600 * 1000).toISOString()
}
