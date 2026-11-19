import { useEffect, useState } from 'react'
import { api, fmtTime, fmtWindow, JSONObj, statusTone } from '../lib/api'
import { useApp } from '../lib/store'
import { PageHead, StatusPill } from '../components/ui'
import { RangeChips } from '../components/charts'

export default function IncidentsPage() {
  const { refreshKey, toast, run, bump } = useApp()
  const [incidents, setIncidents] = useState<JSONObj[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<JSONObj | null>(null)
  const [note, setNote] = useState('')

  const load = () =>
    api.get<JSONObj[]>('/api/incidents').then((data) => {
      setIncidents(data)
      if (!selected && data.length) setSelected(data[0].id)
    })

  useEffect(() => {
    load().catch((err) => toast(String(err), 'bad'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  useEffect(() => {
    if (!selected) return
    api.get<JSONObj>(`/api/incidents/${selected}`).then(setDetail).catch((err) => toast(String(err), 'bad'))
  }, [selected, refreshKey, toast])

  const refresh = async () => {
    await load()
    if (selected) {
      const d = await api.get<JSONObj>(`/api/incidents/${selected}`)
      setDetail(d)
    }
    bump()
  }

  return (
    <>
      <PageHead
        title="事故指挥"
        lead="事故关联影响区间与原因证据；关闭必须对应已完成的恢复评估。关闭前复查若发现新的未解决缺口，事故自动重新打开，旧恢复结论作废。相同原因在关闭后再次出现会形成新区间并关联既往事故。"
        actions={
          <span className="pill info">
            <span className="pd" /> 恢复确认略显保守
          </span>
        }
      />

      <div className="split-2">
        <div className="panel">
          <div className="panel-head">
            <h2>事故列表</h2>
            <span className="hint">{incidents.length} 条</span>
          </div>
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr>
                  <th>编号</th>
                  <th>来源 / 原因</th>
                  <th>状态</th>
                  <th>区间</th>
                  <th>关联</th>
                </tr>
              </thead>
              <tbody>
                {incidents.map((inc) => (
                  <tr key={inc.id} className={`click ${inc.id === selected ? 'selected' : ''}`} onClick={() => setSelected(inc.id)}>
                    <td>
                      <div className="mono" style={{ fontWeight: 560, fontSize: 11.5 }}>
                        {inc.code}
                      </div>
                      <div className="note">{fmtTime(inc.opened_at)}</div>
                    </td>
                    <td>
                      {inc.source_name}
                      <div className="note">{inc.cause_category === 'data_gap' ? '数据缺失' : '核验缺口'}</div>
                    </td>
                    <td>
                      <StatusPill status={inc.status} />
                    </td>
                    <td>
                      <RangeChips ranges={inc.ranges} />
                    </td>
                    <td className="note">
                      {inc.prior_incident_id ? `既往 #${inc.prior_incident_id}` : '—'}
                    </td>
                  </tr>
                ))}
                {!incidents.length && (
                  <tr>
                    <td colSpan={5} className="empty">
                      当前没有事故
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>处置动作</h2>
            {detail && <StatusPill status={detail.status} />}
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 10 }}>
            {detail ? (
              <>
                <dl className="kv">
                  <dt>编号</dt>
                  <dd className="mono">{detail.code}</dd>
                  <dt>来源</dt>
                  <dd>{detail.source_name}</dd>
                  <dt>首次缺口</dt>
                  <dd>{fmtTime(detail.first_breach_at)}</dd>
                  <dt>最近缺口</dt>
                  <dd>{fmtTime(detail.last_breach_at)}</dd>
                  <dt>恢复评估</dt>
                  <dd>{detail.recovery_check_id ? `#${detail.recovery_check_id}` : '未执行'}</dd>
                  <dt>关闭时间</dt>
                  <dd>{detail.closed_at ? fmtTime(detail.closed_at) : '—'}</dd>
                </dl>
                <div className="banner warn">
                  <span>
                    关闭动作只接受与本次缺口区间对应的“已完成恢复评估”。关闭前复查发现新缺口时系统拒绝关闭并重新打开事故。
                  </span>
                </div>
                <div className="row wrap">
                  <button
                    className="btn"
                    disabled={detail.status === 'closed'}
                    onClick={async () => {
                      const res = await run(
                        api.post(`/api/incidents/${detail.id}/recovery-check`, {}),
                        '恢复评估已完成',
                      )
                      if (res) refresh()
                    }}
                  >
                    执行恢复评估
                  </button>
                  <button
                    className="btn primary"
                    disabled={detail.status === 'closed'}
                    onClick={async () => {
                      const res = (await run(
                        api.post(`/api/incidents/${detail.id}/close`, { note: '人工关闭（需通过恢复评估）' }),
                      )) as JSONObj | null
                      if (res) {
                        if (res.ok) toast('事故已关闭')
                        else toast(res.error || '无法关闭', 'bad')
                        refresh()
                      }
                    }}
                  >
                    尝试关闭
                  </button>
                </div>
                <label className="field">
                  添加处置备注
                  <textarea
                    className="input"
                    rows={2}
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="记录处置动作与证据"
                  />
                </label>
                <button
                  className="btn"
                  onClick={async () => {
                    if (!note.trim()) return
                    const res = await run(
                      api.post(`/api/incidents/${detail.id}/events`, { message: note.trim() }),
                      '备注已记录',
                    )
                    if (res) {
                      setNote('')
                      refresh()
                    }
                  }}
                >
                  记录备注
                </button>
              </>
            ) : (
              <div className="empty">选择左侧事故</div>
            )}
          </div>
        </div>
      </div>

      {detail && (
        <div className="split-2" style={{ marginTop: 14 }}>
          <div className="panel">
            <div className="panel-head">
              <h2>事故时间线</h2>
              <span className="hint">{detail.events?.length ?? 0} 个事件</span>
            </div>
            <div className="panel-body">
              <div className="timeline">
                {(detail.events ?? []).map((e: JSONObj) => (
                  <div key={e.id} className={`tl-item ${toneForEvent(e.kind)}`}>
                    <div className="tl-head">
                      <span className="tl-time">{fmtTime(e.at)}</span>
                      <span className="tag">{e.kind}</span>
                    </div>
                    <div className="tl-msg">{e.message}</div>
                    {e.payload?.new_slots?.length ? (
                      <div className="tl-meta">新增槽位：{e.payload.new_slots.slice(0, 8).join('、')}</div>
                    ) : null}
                    {e.payload?.impacts?.length ? (
                      <div className="tl-meta">
                        受影响消费者：{e.payload.impacts.map((i: JSONObj) => `${i.status}/${i.affected_slots}`).join(' · ')}
                      </div>
                    ) : null}
                  </div>
                ))}
                {!detail.events?.length && <div className="empty">暂无事件</div>}
              </div>
            </div>
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2>影响区间与恢复证据</h2>
            </div>
            <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
              <div>
                <h3 style={{ marginBottom: 6 }}>影响区间</h3>
                <div className="chips">
                  {(detail.ranges ?? []).map((r: JSONObj, i: number) => (
                    <span key={i} className="tag">
                      {fmtWindow(r.from, r.to)} · {r.slots?.length ?? 0} 槽
                    </span>
                  ))}
                </div>
              </div>
              <div>
                <h3 style={{ marginBottom: 6 }}>恢复评估</h3>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>窗口</th>
                      <th>未解决事实</th>
                      <th>缺失/待定</th>
                      <th>覆盖</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(detail.recovery_checks ?? []).map((c: JSONObj) => (
                      <tr key={c.id}>
                        <td className="num">{c.id}</td>
                        <td className="mono">{fmtWindow(c.window_start, c.window_end)}</td>
                        <td className="num">{c.evidence?.unresolved_facts ?? '—'}</td>
                        <td className="num">
                          {c.evidence?.missing_slots ?? 0} / {c.evidence?.pending_slots ?? 0}
                        </td>
                        <td className="num">
                          {c.evidence?.coverage_num}/{c.evidence?.coverage_den}
                        </td>
                      </tr>
                    ))}
                    {!detail.recovery_checks?.length && (
                      <tr>
                        <td colSpan={5} className="empty">
                          尚未执行恢复评估
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              <div>
                <h3 style={{ marginBottom: 6 }}>原因证据</h3>
                <div className="chips">
                  {(detail.evidence ?? [])
                    .filter((e: JSONObj) => e.kind === 'consumer_impact')
                    .slice(-1)
                    .flatMap((e: JSONObj) => e.impacts ?? [])
                    .map((i: JSONObj, idx: number) => (
                      <span key={idx} className="tag">
                        消费者 #{i.consumer_id} · {i.status} · {i.affected_slots} 槽
                      </span>
                    ))}
                </div>
                <details className="raw" style={{ marginTop: 8 }}>
                  <summary>查看完整证据 JSON</summary>
                  <pre>{JSON.stringify(detail.evidence, null, 2)}</pre>
                </details>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

function toneForEvent(kind: string): string {
  if (['closed', 'recovery_check_completed'].includes(kind)) return 'good'
  if (['opened', 'extended', 'reopened', 'reopened_as_new_incident'].includes(kind)) return 'bad'
  if (['recovery_candidate', 'close_blocked_new_gap', 'close_blocked_stale_check', 'consumer_impact'].includes(kind)) return 'warn'
  return 'info'
}
