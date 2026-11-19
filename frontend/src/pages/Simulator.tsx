import { useEffect, useState } from 'react'
import { api, fmtTime, JSONObj } from '../lib/api'
import { useApp } from '../lib/store'
import { PageHead } from '../components/ui'

export default function SimulatorPage() {
  const { meta, clock, refreshKey, run, toast, bump, reloadMeta } = useApp()
  const [sources, setSources] = useState<JSONObj[]>([])
  const [timeline, setTimeline] = useState<JSONObj[]>([])
  const [meter, setMeter] = useState('M-ELE-B2-01')
  const [from, setFrom] = useState('2026-11-10T09:00')
  const [to, setTo] = useState('2026-11-10T10:00')
  const [backfillSlot, setBackfillSlot] = useState('2026-11-10T09:00')
  const [backfillValue, setBackfillValue] = useState('11800.5')
  const [delayMinutes, setDelayMinutes] = useState(120)

  const load = async () => {
    const [s, t] = await Promise.all([api.get<JSONObj[]>('/api/sources'), api.get<JSONObj[]>('/api/sim/timeline?limit=60')])
    setSources(s)
    setTimeline(t)
  }

  useEffect(() => {
    load().catch((err) => toast(String(err), 'bad'))
  }, [refreshKey, toast])

  const meters = sources.flatMap((s) => s.meters.map((m: JSONObj) => ({ ...m, source: s.name })))

  const act = async (payload: JSONObj, okText?: string) => {
    const res = await run(api.post<JSONObj>('/api/sim/action', payload), okText)
    if (res) {
      await reloadMeta()
      bump()
      load()
    }
  }

  const advance = async (minutes: number) => {
    const res = await run(api.post<JSONObj>('/api/sim/clock', { minutes, run: true }), `时钟前进 ${minutes} 分钟并重算`)
    if (res) {
      await reloadMeta()
      bump()
      load()
    }
  }

  return (
    <>
      <PageHead
        title="模拟与验收"
        lead="从模拟表计的实际到达开始操作：推进时钟让表计继续发射，延迟/丢弃样本、补传、登记维护和换频都走与调度器相同的完整重算路径。"
        actions={
          <>
            <span className="tag">模拟时钟 {fmtTime(clock?.now)}</span>
            <div className="seg">
              {[15, 60, 180].map((m) => (
                <button key={m} onClick={() => advance(m)}>
                  +{m}分
                </button>
              ))}
            </div>
            <button
              className="btn"
              onClick={async () => {
                const res = await run(api.post('/api/acceptance/reset', {}), '已重置为基线')
                if (res) {
                  await reloadMeta()
                  bump()
                  load()
                }
              }}
            >
              重置为基线
            </button>
          </>
        }
      />

      <div className="split-3">
        <div className="panel">
          <div className="panel-head">
            <h2>样本到达 / 延迟</h2>
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 10 }}>
            <label className="field">
              表计
              <select className="input" value={meter} onChange={(e) => setMeter(e.target.value)}>
                {meters.map((m: JSONObj) => (
                  <option key={m.code} value={m.code}>
                    {m.code} · {m.name}（{m.source}）
                  </option>
                ))}
              </select>
            </label>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                区间起
                <input className="input" value={from} onChange={(e) => setFrom(e.target.value)} />
              </label>
              <label className="field" style={{ flex: 1 }}>
                区间止
                <input className="input" value={to} onChange={(e) => setTo(e.target.value)} />
              </label>
            </div>
            <label className="field">
              延迟分钟数
              <input className="input" type="number" value={delayMinutes} onChange={(e) => setDelayMinutes(Number(e.target.value))} />
            </label>
            <div className="row">
              <button
                className="btn"
                onClick={() =>
                  act(
                    { action: 'delay', meter, from, to, minutes: delayMinutes },
                    '样本已延迟到达并重算受影响窗口',
                  )
                }
              >
                设置迟到
              </button>
              <button
                className="btn danger"
                onClick={() => act({ action: 'drop', meter, from, to }, '样本已丢弃，窗口将出现缺失')}
              >
                丢弃样本
              </button>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>补传 / 迟到更正</h2>
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 10 }}>
            <label className="field">
              表计
              <select className="input" value={meter} onChange={(e) => setMeter(e.target.value)}>
                {meters.map((m: JSONObj) => (
                  <option key={m.code} value={m.code}>
                    {m.code} · {m.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              槽位开始
              <input className="input" value={backfillSlot} onChange={(e) => setBackfillSlot(e.target.value)} />
            </label>
            <label className="field">
              读数 / 用量
              <input className="input" value={backfillValue} onChange={(e) => setBackfillValue(e.target.value)} />
            </label>
            <button
              className="btn primary"
              onClick={() =>
                act(
                  {
                    action: 'backfill',
                    meter,
                    slot: backfillSlot,
                    value: Number(backfillValue),
                    note: '迟到补传（模拟）',
                  },
                  '补传已入库并触发受影响窗口完整重算',
                )
              }
            >
              迟到补传
            </button>
            <div className="note">补传不修改既有违约事实；只解除缺口并更新受影响窗口的评估。</div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>维护 / 换频</h2>
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 10 }}>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                维护起
                <input className="input" value={from} onChange={(e) => setFrom(e.target.value)} />
              </label>
              <label className="field" style={{ flex: 1 }}>
                维护止
                <input className="input" value={to} onChange={(e) => setTo(e.target.value)} />
              </label>
            </div>
            <button
              className="btn"
              onClick={() =>
                act(
                  { action: 'maintenance', meter, from, to, allowed_deduction: true, reason: '模拟维护' },
                  '维护窗口已登记（登记时间=当前模拟时钟）',
                )
              }
            >
              登记维护（当前时间登记）
            </button>
            <button
              className="btn"
              onClick={() =>
                act(
                  {
                    action: 'frequency',
                    meter,
                    from,
                    cadence_minutes: 30,
                    allowed_lateness_minutes: 30,
                    note: '模拟换频',
                  },
                  '采样频率已变更，窗口按段重算',
                )
              }
            >
              切换为 30 分钟频率
            </button>
            <div className="note">维护登记晚于槽位截止时，属于事后补录：分母可扣除，但当时违约事实保留。</div>
          </div>
        </div>
      </div>

      <div className="panel" style={{ marginTop: 14 }}>
        <div className="panel-head">
          <h2>审计时间线</h2>
          <span className="hint">采样到达、重算、维护登记与时钟推进</span>
        </div>
        <div className="scroll-x">
          <table className="tbl">
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>目标</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {timeline.map((t) => (
                <tr key={t.id}>
                  <td className="mono">{fmtTime(t.at)}</td>
                  <td>
                    <span className="tag">{t.action}</span>
                  </td>
                  <td>{t.target}</td>
                  <td className="note mono-wrap" style={{ maxWidth: 520 }}>
                    {JSON.stringify(t.detail)}
                  </td>
                </tr>
              ))}
              {!timeline.length && (
                <tr>
                  <td colSpan={4} className="empty">
                    暂无审计记录
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
