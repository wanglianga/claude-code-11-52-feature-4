import { useEffect, useState } from 'react'
import { api, fmtTime, JSONObj } from '../lib/api'
import { useApp } from '../lib/store'
import { PageHead } from '../components/ui'

export default function RegistryPage() {
  const { refreshKey, run, toast, clock } = useApp()
  const [sources, setSources] = useState<JSONObj[]>([])
  const [active, setActive] = useState<string>('')
  const [showMaint, setShowMaint] = useState(false)
  const [showCommitment, setShowCommitment] = useState(false)

  const load = () =>
    api.get<JSONObj[]>('/api/sources').then((data) => {
      setSources(data)
      if (!active && data.length) setActive(data[0].code)
    })

  useEffect(() => {
    load().catch((err) => toast(String(err), 'bad'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  const source = sources.find((s) => s.code === active)

  return (
    <>
      <PageHead
        title="来源登记"
        lead="维护每类数据的采样承诺：表计有效安装期、计划频率、允许迟到、维护生效区间与产品依赖。预期槽位按窗口实际推导；事后补录维护不能抹去当时已发生的违约事实。"
        actions={
          <>
            <button className="btn" onClick={() => setShowCommitment(true)} disabled={!source}>
              变更采样频率
            </button>
            <button className="btn primary" onClick={() => setShowMaint(true)} disabled={!source}>
              登记维护窗口
            </button>
          </>
        }
      />

      <div className="panel">
        <div className="panel-head">
          <div className="chips">
            {sources.map((s) => (
              <button key={s.code} className={`chip ${s.code === active ? 'on' : ''}`} onClick={() => setActive(s.code)}>
                {s.name} · {s.unit}
              </button>
            ))}
          </div>
          <span className="hint">{source ? `${source.meters.length} 个表计` : ''}</span>
        </div>
        <div className="scroll-x">
          <table className="tbl">
            <thead>
              <tr>
                <th>表计</th>
                <th>楼栋</th>
                <th>口径 / 倍率</th>
                <th>采样频率</th>
                <th>允许迟到</th>
                <th>有效安装期</th>
                <th>维护窗口</th>
              </tr>
            </thead>
            <tbody>
              {(source?.meters ?? []).map((m: JSONObj) => (
                <tr key={m.id}>
                  <td>
                    <div style={{ fontWeight: 540 }}>{m.code}</div>
                    <div className="note">{m.name}</div>
                  </td>
                  <td>
                    {m.building_code} {m.building_name}
                  </td>
                  <td>
                    {m.cumulative ? '累计表码' : '区间用量'} · ×{m.multiplier}
                    {m.linked_from_meter_id ? <div className="note">换表自 #{m.linked_from_meter_id}，倍率 {m.link_ratio}</div> : null}
                  </td>
                  <td className="num">{m.cadence_minutes} 分钟</td>
                  <td className="num">{m.allowed_lateness_minutes} 分钟</td>
                  <td className="mono note">
                    {m.valid_from?.slice(0, 16).replace('T', ' ')} → {m.valid_to ? m.valid_to.slice(0, 16).replace('T', ' ') : '长期'}
                  </td>
                  <td>
                    {m.maintenance?.length ? (
                      <div style={{ display: 'grid', gap: 3 }}>
                        {m.maintenance.map((w: JSONObj) => (
                          <span key={w.id} className="tag">
                            {w.starts_at?.slice(11, 16)}–{w.ends_at?.slice(11, 16)} · {w.allowed_deduction ? '可扣除' : '不扣除'}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <span className="note">无</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid split-2" style={{ marginTop: 14 }}>
        <div className="panel">
          <div className="panel-head">
            <h2>采样承诺历史</h2>
            <span className="hint">频率/安装关系改变按段计算分母</span>
          </div>
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr>
                  <th>表计</th>
                  <th>生效</th>
                  <th>失效</th>
                  <th>频率</th>
                  <th>允许迟到</th>
                </tr>
              </thead>
              <tbody>
                {(source?.commitments ?? []).map((c: JSONObj) => (
                  <tr key={c.commitment_id}>
                    <td>{c.meter_code}</td>
                    <td className="mono note">{c.valid_from?.slice(5, 16).replace('T', ' ')}</td>
                    <td className="mono note">{c.valid_to ? c.valid_to.slice(5, 16).replace('T', ' ') : '长期'}</td>
                    <td className="num">{c.cadence_minutes} 分钟</td>
                    <td className="num">{c.allowed_lateness_minutes} 分钟</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div className="panel">
          <div className="panel-head">
            <h2>产品依赖</h2>
            <span className="hint">该来源字段被哪些产品/消费者依赖</span>
          </div>
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr>
                  <th>产品</th>
                  <th>消费者</th>
                  <th>楼栋</th>
                  <th>生效</th>
                </tr>
              </thead>
              <tbody>
                {(source?.product_dependencies ?? []).map((d: JSONObj, i: number) => (
                  <tr key={i}>
                    <td>{d.product_name}</td>
                    <td>{d.consumer_name}</td>
                    <td>{d.building_code ?? '全部'}</td>
                    <td className="mono note">{d.valid_from?.slice(0, 10)}</td>
                  </tr>
                ))}
                {!source?.product_dependencies?.length && (
                  <tr>
                    <td colSpan={4} className="empty">
                      该字段暂无消费者依赖
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="panel-body" style={{ borderTop: '1px solid var(--line)' }}>
            <div className="note">
              维护登记时间晚于槽位截止时视为“事后补录”：分母照常扣除，但违约事实保留并标注。修改采样承诺会触发当前模拟日全部已完成窗口重算。
            </div>
          </div>
        </div>
      </div>

      {showMaint && source && (
        <MaintenanceDialog
          source={source}
          clock={clock}
          onClose={() => setShowMaint(false)}
          onSubmit={async (payload) => {
            const res = await run(api.post(`/api/sources/${source.code}/maintenance`, payload), '维护窗口已登记并重算')
            if (res) {
              setShowMaint(false)
              load()
            }
          }}
        />
      )}
      {showCommitment && source && (
        <CommitmentDialog
          source={source}
          onClose={() => setShowCommitment(false)}
          onSubmit={async (payload) => {
            const res = await run(api.post(`/api/sources/${source.code}/commitments`, payload), '采样承诺已更新并拆段重算')
            if (res) {
              setShowCommitment(false)
              load()
            }
          }}
        />
      )}
    </>
  )
}

function MaintenanceDialog({
  source,
  clock,
  onClose,
  onSubmit,
}: {
  source: JSONObj
  clock: JSONObj | null
  onClose: () => void
  onSubmit: (payload: JSONObj) => void
}) {
  const [meter, setMeter] = useState(source.meters[0]?.code ?? '')
  const [from, setFrom] = useState('2026-11-10T09:00')
  const [to, setTo] = useState('2026-11-10T10:00')
  const [allowed, setAllowed] = useState(true)
  const [reason, setReason] = useState('计划维护')
  const [recordedAt, setRecordedAt] = useState(clock?.now?.slice(0, 16).replace('Z', '') ?? '')

  return (
    <Overlay onClose={onClose} title="登记维护窗口">
      <div className="grid" style={{ gap: 10 }}>
        <label className="field">
          表计
          <select className="input" value={meter} onChange={(e) => setMeter(e.target.value)}>
            {source.meters.map((m: JSONObj) => (
              <option key={m.code} value={m.code}>
                {m.code} · {m.name}
              </option>
            ))}
          </select>
        </label>
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            开始
            <input className="input" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label className="field" style={{ flex: 1 }}>
            结束
            <input className="input" value={to} onChange={(e) => setTo(e.target.value)} />
          </label>
        </div>
        <label className="field">
          登记时间（晚于槽位截止即为事后补录）
          <input className="input" value={recordedAt} onChange={(e) => setRecordedAt(e.target.value)} />
        </label>
        <label className="row" style={{ fontSize: 12.5 }}>
          <input type="checkbox" checked={allowed} onChange={(e) => setAllowed(e.target.checked)} />
          规则允许扣除这些槽位（否则不扣分母）
        </label>
        <label className="field">
          原因
          <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            onClick={() =>
              onSubmit({
                meter,
                from,
                to,
                allowed_deduction: allowed,
                reason,
                recorded_at: recordedAt || undefined,
              })
            }
          >
            提交并重算
          </button>
        </div>
      </div>
    </Overlay>
  )
}

function CommitmentDialog({
  source,
  onClose,
  onSubmit,
}: {
  source: JSONObj
  onClose: () => void
  onSubmit: (payload: JSONObj) => void
}) {
  const [meter, setMeter] = useState(source.meters[0]?.code ?? '')
  const [from, setFrom] = useState('2026-11-10T09:00')
  const [cadence, setCadence] = useState(30)
  const [lateness, setLateness] = useState(30)

  return (
    <Overlay onClose={onClose} title="变更采样频率">
      <div className="grid" style={{ gap: 10 }}>
        <label className="field">
          表计
          <select className="input" value={meter} onChange={(e) => setMeter(e.target.value)}>
            {source.meters.map((m: JSONObj) => (
              <option key={m.code} value={m.code}>
                {m.code} · {m.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          生效时间
          <input className="input" value={from} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <div className="row">
          <label className="field" style={{ flex: 1 }}>
            新频率（分钟）
            <input className="input" type="number" value={cadence} onChange={(e) => setCadence(Number(e.target.value))} />
          </label>
          <label className="field" style={{ flex: 1 }}>
            允许迟到（分钟）
            <input className="input" type="number" value={lateness} onChange={(e) => setLateness(Number(e.target.value))} />
          </label>
        </div>
        <div className="note">窗口跨过生效时间时，前后按各自频率拆段计算分母。</div>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            onClick={() =>
              onSubmit({
                meter,
                valid_from: from,
                cadence_minutes: cadence,
                allowed_lateness_minutes: lateness,
              })
            }
          >
            提交并重算
          </button>
        </div>
      </div>
    </Overlay>
  )
}

function Overlay({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(16,19,24,0.42)',
        display: 'grid',
        placeItems: 'center',
        zIndex: 30,
      }}
      onClick={onClose}
    >
      <div
        style={{ width: 460, background: '#fff', borderRadius: 12, boxShadow: '0 20px 60px rgba(0,0,0,0.2)' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="panel-head">
          <h2>{title}</h2>
          <button className="btn ghost sm" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="panel-body">{children}</div>
      </div>
    </div>
  )
}
