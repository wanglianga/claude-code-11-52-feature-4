import { Link, useLocation } from 'react-router-dom'
import { ReactNode } from 'react'
import { useApp } from '../lib/store'
import { fmtTime, statusTone, STATUS_LABEL, setToken } from '../lib/api'

const NAV = [
  { group: '总览', items: [{ to: '/', label: '产品健康', ico: '◉' }] },
  {
    group: '数据产品',
    items: [
      { to: '/products', label: '产品运行', ico: '▤' },
      { to: '/demand', label: '峰值需量证据', ico: '◭' },
      { to: '/registry', label: '来源登记', ico: '⌗' },
    ],
  },
  {
    group: '消费与事故',
    items: [
      { to: '/impact', label: '消费影响', ico: '⇄' },
      { to: '/incidents', label: '事故指挥', ico: '⚑' },
    ],
  },
  {
    group: '验证',
    items: [
      { to: '/simulator', label: '模拟与验收', ico: '▶' },
      { to: '/acceptance', label: '验收报告', ico: '✓' },
    ],
  },
]

export function Layout({ children, user }: { children: ReactNode; user: { display_name: string; role: string } }) {
  const { clock } = useApp()
  const loc = useLocation()
  const current = NAV.flatMap((g) => g.items).find(
    (i) => i.to === loc.pathname || (i.to !== '/' && loc.pathname.startsWith(i.to)),
  )

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="mark">
            <div className="dot">SLO</div>
            <div>
              <div className="name">能耗数据产品守护台</div>
            </div>
          </div>
          <div className="sub">园区能源管理中心</div>
        </div>
        <nav className="nav">
          {NAV.map((group) => (
            <div key={group.group}>
              <div className="nav-group">{group.group}</div>
              {group.items.map((item) => (
                <Link
                  key={item.to}
                  to={item.to}
                  className={item.to === loc.pathname || (item.to !== '/' && loc.pathname.startsWith(item.to)) ? 'active' : ''}
                >
                  <span className="ico">{item.ico}</span>
                  {item.label}
                </Link>
              ))}
            </div>
          ))}
        </nav>
        <div className="side-foot">
          <div className="clockbox">
            <span>模拟时钟</span>
            <span className="t">{fmtTime(clock?.now)}</span>
          </div>
          <div style={{ marginTop: 4 }}>观察窗口 {clock?.window_hours ?? 6}h · 15min 槽位</div>
        </div>
      </aside>
      <div className="main">
        <div className="topbar">
          <div className="crumbs">
            <span>园区能源管理中心</span>
            <span>/</span>
            <b>{current?.label ?? '产品健康'}</b>
          </div>
          <div className="right">
            <span className="pill info">
              <span className="pd" /> 窗口完整重算
            </span>
            <span className="user-chip">
              <span className="avatar">{user.display_name.slice(0, 1)}</span>
              {user.display_name}
              <span className="tag">{user.role}</span>
            </span>
            <button
              className="btn ghost sm"
              onClick={() => {
                setToken(null)
                window.location.reload()
              }}
            >
              退出
            </button>
          </div>
        </div>
        <div className="content">{children}</div>
      </div>
    </div>
  )
}

export function StatusPill({ status, label }: { status?: string; label?: string }) {
  const tone = statusTone(status)
  return (
    <span className={`pill ${tone}`}>
      <span className="pd" />
      {label ?? STATUS_LABEL[status ?? ''] ?? status ?? '—'}
    </span>
  )
}

export function PageHead({
  title,
  lead,
  actions,
}: {
  title: string
  lead?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {lead && <div className="lead">{lead}</div>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </div>
  )
}
