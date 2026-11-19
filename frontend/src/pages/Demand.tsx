import { useEffect, useMemo, useState } from 'react'
import { api, fmtTime, JSONObj } from '../lib/api'
import { useApp } from '../lib/store'
import { PageHead } from '../components/ui'

const CONTRACT_LABEL: Record<string, string> = {
  exceeded: '已证实超限',
  not_exceeded: '已证实未超限',
  not_proven: '尚未证实超限',
  no_contract: '未登记合同容量',
}

const RISK_LABEL: Record<string, string> = {
  none: '无未知风险',
  bounded: '未知范围有保守上界',
  unbounded: '未知范围无上界',
}

const SLOT_STATUS_LABEL: Record<string, string> = {
  full: '完整覆盖',
  partial: '部分覆盖',
  none: '无覆盖',
}

function contractTone(status: string): 'good' | 'warn' | 'bad' | 'muted' {
  if (status === 'exceeded') return 'bad'
  if (status === 'not_exceeded') return 'good'
  if (status === 'not_proven') return 'warn'
  return 'muted'
}

function kw(v?: number | null): string {
  return v == null ? '—' : v.toFixed(1)
}

function clockOf(iso?: string | null): string {
  if (!iso) return '—'
  return iso.slice(11, 16)
}

export default function DemandPage() {
  const { refreshKey, toast, run } = useApp()
  const [meta, setMeta] = useState<JSONObj | null>(null)
  const [data, setData] = useState<JSONObj | null>(null)
  const [building, setBuilding] = useState('')
  const [method, setMethod] = useState('grid')
  const [detail, setDetail] = useState<JSONObj | null>(null)
  const [versionsOnly, setVersionsOnly] = useState(false)

  useEffect(() => {
    api
      .get<JSONObj>('/api/demand/meta')
      .then(setMeta)
      .catch((err) => toast(String(err), 'bad'))
  }, [refreshKey, toast])

  const load = async () => {
    try {
      const res = await api.get<JSONObj>('/api/demand/reports?recompute=true')
      setData(res)
      if (!building && res.reports?.length) setBuilding(res.reports[0].building_code)
    } catch (err) {
      toast(String(err), 'bad')
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  const reports = (data?.reports ?? []) as JSONObj[]
  const buildingReports = useMemo(
    () => reports.filter((r) => r.building_code === building),
    [reports, building],
  )
  const activeReport =
    buildingReports.find((r) => r.method === method) ?? buildingReports[0]

  useEffect(() => {
    if (!activeReport) {
      setDetail(null)
      return
    }
    api
      .get<JSONObj>(`/api/demand/reports/${activeReport.id}`)
      .then(setDetail)
      .catch((err) => toast(String(err), 'bad'))
  }, [activeReport?.id, toast])

  const publishWithCutoff = async (cutoff: string) => {
    const res = await run(
      api.post<JSONObj>('/api/demand/reports', { cutoff }),
      `已按截止点 ${fmtTime(cutoff)} 发布报告`,
    )
    if (res) await load()
  }

  const replay = async () => {
    const res = await run(
      api.post<JSONObj>('/api/demand/acceptance/run', {}),
      '峰值需量验收场景已重放',
    )
    if (res) {
      await load()
      toast('逐格覆盖、缺口候选与迟到峰值版本均已从数据库读回')
    }
  }

  return (
    <>
      <PageHead
        title="峰值需量证据"
        lead="以表计实际采样覆盖、功率/电量单位、15 分钟结算格与合同容量为依据，给出每栋楼在报告期内已确认峰值、峰值候选时间格、缺口格与合同超限的可证实/未知范围。只供容量复核：不执行限电，也不直接计费。"
        actions={
          <>
            <div className="seg">
              {['grid', 'integration'].map((m) => (
                <button key={m} className={method === m ? 'on' : ''} onClick={() => setMethod(m)}>
                  {m === 'grid' ? '15 分钟格' : '区间积分'}
                </button>
              ))}
            </div>
            <button className="btn" onClick={replay}>
              重放峰值验收
            </button>
          </>
        }
      />

      <div className="banner info" style={{ marginBottom: 12 }}>
        <span>
          口径绑定同一数据截止点 <code>{fmtTime(data?.data_cutoff)}</code>：读数按采样时间归格（接收时间只用于判断是否已到达），
          平均需量只在完整覆盖的格上确认；部分覆盖只作保守下界，累计表回退与断档列为未知。
          最高已确认格低于合同但存在缺口格时，结论只会是「尚未证实超限」。
        </span>
      </div>

      <div className="split-2">
        <div className="panel">
          <div className="panel-head">
            <h2>楼栋合同状态</h2>
            <span className="hint">
              {data?.period ? `${fmtTime(data.period.start)} → ${fmtTime(data.period.end)}` : ''}
            </span>
          </div>
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr>
                  <th>楼栋</th>
                  <th>口径</th>
                  <th>已确认峰值</th>
                  <th>风险区间 (kW)</th>
                  <th>合同</th>
                  <th>结论</th>
                </tr>
              </thead>
              <tbody>
                {(reports as JSONObj[]).map((r) => (
                  <tr
                    key={r.id}
                    className={`click ${r.building_code === building && r.method === method ? 'selected' : ''}`}
                    onClick={() => {
                      setBuilding(r.building_code)
                      setMethod(r.method)
                    }}
                  >
                    <td>
                      <div style={{ fontWeight: 560 }}>{r.building_code}</div>
                      <div className="note">{r.building_name}</div>
                    </td>
                    <td className="note">{r.method === 'grid' ? '格归集' : '积分'}</td>
                    <td className="num">
                      {kw(r.confirmed_peak_kw)}
                      <div className="note">
                        {clockOf(r.confirmed_peak_slot)}
                        {r.confirmed_peak_slot ? `-${clockOf(addMinutes(r.confirmed_peak_slot, 15))}` : ''}
                      </div>
                    </td>
                    <td className="num">
                      [{kw(r.risk_interval?.lower_kw)}, {kw(r.risk_interval?.upper_kw)}]
                    </td>
                    <td className="num">{kw(r.contract_kw)}</td>
                    <td>
                      <span className={`pill ${contractTone(r.contract_status)}`}>
                        <span className="pd" />
                        {CONTRACT_LABEL[r.contract_status] ?? r.contract_status}
                      </span>
                      <div className="note">v{r.version} · r{r.revision}</div>
                    </td>
                  </tr>
                ))}
                {!reports.length && (
                  <tr>
                    <td colSpan={6} className="empty">
                      暂无峰值需量报告
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>结论与范围</h2>
            {activeReport && (
              <span className="hint">
                {activeReport.building_code} · {activeReport.method_label}
              </span>
            )}
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 10 }}>
            {activeReport ? (
              <>
                <div className="row wrap">
                  <span className={`pill ${contractTone(activeReport.contract_status)}`}>
                    <span className="pd" />
                    {CONTRACT_LABEL[activeReport.contract_status]}
                  </span>
                  <span className={`pill ${activeReport.unknown_risk === 'none' ? 'good' : activeReport.unknown_risk === 'bounded' ? 'warn' : 'bad'}`}>
                    {RISK_LABEL[activeReport.unknown_risk]}
                  </span>
                  {activeReport.delta && (
                    <span className="pill info">版本变化</span>
                  )}
                </div>
                <div style={{ fontSize: 12.5 }}>{activeReport.conclusion}</div>
                <dl className="kv">
                  <dt>已确认峰值</dt>
                  <dd>
                    {kw(activeReport.confirmed_peak_kw)} kW
                    {activeReport.confirmed_peak_slot ? ` @ ${clockOf(activeReport.confirmed_peak_slot)}` : ''}
                  </dd>
                  <dt>下界 / 上界</dt>
                  <dd>
                    {kw(activeReport.risk_interval?.lower_kw)} / {kw(activeReport.risk_interval?.upper_kw)} kW
                  </dd>
                  <dt>合同容量</dt>
                  <dd>{kw(activeReport.contract_kw)} kW</dd>
                  <dt>完整 / 部分 / 无覆盖格</dt>
                  <dd>
                    {activeReport.counts?.full ?? 0} / {activeReport.counts?.partial ?? 0} /{' '}
                    {activeReport.counts?.none ?? 0}
                  </dd>
                  <dt>未知候选峰值格</dt>
                  <dd>{activeReport.candidate_peak_slots?.length ?? 0}</dd>
                  <dt>重复报文 / 回退</dt>
                  <dd>
                    {activeReport.counts?.duplicates ?? 0} / {activeReport.counts?.rollbacks ?? 0}
                  </dd>
                </dl>
                <div className="note">{activeReport.risk_interval?.explanation}</div>
                {activeReport.warnings?.length > 0 && (
                  <ul className="note" style={{ margin: 0, paddingLeft: 18 }}>
                    {activeReport.warnings.map((w: JSONObj, i: number) => (
                      <li key={i}>{w.detail}</li>
                    ))}
                  </ul>
                )}
                <div className="row wrap" style={{ marginTop: 4 }}>
                  <button className="btn sm" onClick={() => publishWithCutoff('2026-11-10T00:00:00')}>
                    按 00:00 截止点发布
                  </button>
                  <button className="btn sm" onClick={() => publishWithCutoff('2026-11-10T02:00:00')}>
                    按 02:00 截止点发布（含迟到读数）
                  </button>
                  <div className="spacer" />
                  <button className="btn sm ghost" onClick={() => setVersionsOnly((v) => !v)}>
                    {versionsOnly ? '显示逐格明细' : '只看版本谱系'}
                  </button>
                </div>
              </>
            ) : (
              <div className="empty">选择任一报告</div>
            )}
          </div>
        </div>
      </div>

      {activeReport?.delta && (
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="panel-head">
            <h2>峰值结论变化</h2>
            <span className="hint">迟到读数只生成新版本</span>
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 8 }}>
            <div className="diff" style={{ gridTemplateColumns: '1fr auto 1fr' }}>
              <div className="panel" style={{ boxShadow: 'none', padding: 10 }}>
                <div className="note">上一版本 · 截止 {fmtTime(activeReport.delta.previous_cutoff)}</div>
                <div className="num" style={{ fontSize: 20 }}>{kw(activeReport.delta.confirmed_peak_kw_before)} kW</div>
                <div className="note">
                  {clockOf(activeReport.delta.peak_slot_before)} · {' '}
                  {CONTRACT_LABEL[activeReport.delta.contract_status_before]}
                </div>
              </div>
              <div className="arrow">→</div>
              <div className="panel" style={{ boxShadow: 'none', padding: 10 }}>
                <div className="note">当前版本 · 截止 {fmtTime(activeReport.delta.current_cutoff)}</div>
                <div className="num" style={{ fontSize: 20 }}>{kw(activeReport.delta.confirmed_peak_kw_after)} kW</div>
                <div className="note">
                  {clockOf(activeReport.delta.peak_slot_after)} · {' '}
                  {CONTRACT_LABEL[activeReport.delta.contract_status_after]}
                </div>
              </div>
            </div>
            <div className="note">{activeReport.delta.summary}</div>
          </div>
        </div>
      )}

      {detail && (versionsOnly ? <VersionTable versions={detail.versions} /> : <SlotTable detail={detail} />)}
    </>
  )
}

function addMinutes(iso: string, minutes: number): string {
  return new Date(new Date(iso).getTime() + minutes * 60000).toISOString()
}

function VersionTable({ versions }: { versions: JSONObj[] }) {
  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="panel-head">
        <h2>版本谱系</h2>
        <span className="hint">每个数据截止点一个不可变版本</span>
      </div>
      <div className="scroll-x">
        <table className="tbl">
          <thead>
            <tr>
              <th>版本</th>
              <th>数据截止点</th>
              <th>已确认峰值</th>
              <th>下界 / 上界</th>
              <th>合同状态</th>
              <th>结论变化</th>
              <th>重算</th>
            </tr>
          </thead>
          <tbody>
            {(versions ?? []).map((v) => (
              <tr key={v.id}>
                <td className="num">
                  v{v.version} · r{v.revision}
                </td>
                <td className="num">{fmtTime(v.data_cutoff)}</td>
                <td className="num">{kw(v.confirmed_peak_kw)} kW</td>
                <td className="num">
                  {kw(v.lower_kw)} / {kw(v.upper_kw)}
                </td>
                <td>
                  <span className={`pill ${contractTone(v.contract_status)}`}>
                    {CONTRACT_LABEL[v.contract_status]}
                  </span>
                </td>
                <td className="note">{v.delta?.summary ?? '—'}</td>
                <td className="num">{v.recompute_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SlotTable({ detail }: { detail: JSONObj }) {
  const slots = (detail.slots ?? []) as JSONObj[]
  const candidates = new Set((detail.candidate_peak_slots ?? []).map((c: JSONObj) => c.slot))
  const peak = detail.confirmed_peak?.slot
  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="panel-head">
        <h2>逐格证据 · {detail.building_code} · {detail.method_label}</h2>
        <span className="hint">
          完整覆盖才确认平均值；部分覆盖只给观测下界；无覆盖为未知格
        </span>
      </div>
      <div className="scroll-x">
        <table className="tbl">
          <thead>
            <tr>
              <th>结算格</th>
              <th>覆盖</th>
              <th>15 分钟平均需量</th>
              <th>观测下界</th>
              <th>保守上界</th>
              <th>合同比较</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {slots.map((s) => {
              const reasons = Array.from(
                new Set((s.meters ?? []).flatMap((m: JSONObj) => (m.reasons ?? []).map((r: JSONObj) => r.kind))),
              ) as string[]
              return (
                <tr key={s.slot} className={s.slot === peak ? 'selected' : ''}>
                  <td className="mono">
                    {clockOf(s.slot)}–{clockOf(s.slot_end)}
                    {s.slot === peak && <span className="pill good" style={{ marginLeft: 6 }}>峰值</span>}
                    {candidates.has(s.slot) && <span className="pill warn" style={{ marginLeft: 6 }}>候选</span>}
                  </td>
                  <td>
                    <span className={`pill ${s.status === 'full' ? 'good' : s.status === 'partial' ? 'warn' : 'bad'}`}>
                      <span className="pd" />
                      {SLOT_STATUS_LABEL[s.status] ?? s.status}
                    </span>
                  </td>
                  <td className="num">{s.status === 'full' ? kw(s.demand_kw) : '—'}</td>
                  <td className="num">{kw(s.lower_kw)}</td>
                  <td className="num">{kw(s.upper_kw)}</td>
                  <td>
                    {s.above_contract ? (
                      <span className="pill bad">超限</span>
                    ) : (
                      <span className="note">—</span>
                    )}
                  </td>
                  <td className="note">
                    {reasons.length ? reasons.join(' / ') : s.upper_sources?.[0]?.label ?? ''}
                  </td>
                </tr>
              )
            })}
            {!slots.length && (
              <tr>
                <td colSpan={7} className="empty">
                  该口径没有到期结算格
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {detail.meters?.length > 0 && (
        <div className="panel-body" style={{ borderTop: '1px solid var(--line)' }}>
          <h3 style={{ marginBottom: 6 }}>计量点与单位口径</h3>
          <table className="tbl">
            <thead>
              <tr>
                <th>表计</th>
                <th>模式</th>
                <th>数据流</th>
                <th>读数</th>
                <th>重复</th>
                <th>额定上界</th>
                <th>相邻变化率</th>
              </tr>
            </thead>
            <tbody>
              {detail.meters.map((m: JSONObj) => (
                <tr key={m.meter_code}>
                  <td className="mono">{m.meter_code}</td>
                  <td className="note">{m.mode_label}</td>
                  <td className="note">{m.stream}</td>
                  <td className="num">{m.readings_used}</td>
                  <td className="num">{m.duplicates}</td>
                  <td className="num">{kw(m.rating_kw)}</td>
                  <td className="num">{kw(m.neighbor_step_kw)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
