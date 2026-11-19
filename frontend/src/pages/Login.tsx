import { useState } from 'react'
import { api, setToken, JSONObj } from '../lib/api'

export default function Login({ onLogin }: { onLogin: (user: { display_name: string; role: string }) => void }) {
  const [username, setUsername] = useState('ops')
  const [password, setPassword] = useState('ops123')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const res = await api.post<JSONObj>('/api/auth/login', { username, password })
      setToken(res.token)
      localStorage.setItem('park-slo-user', JSON.stringify(res.user))
      onLogin(res.user)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="row" style={{ gap: 10, marginBottom: 6 }}>
          <div className="brand dot" style={{ background: 'linear-gradient(140deg,#5b8cff,#1f4fae)' }}>
            <div className="dot" style={{ width: 30, height: 30 }}>
              SLO
            </div>
          </div>
          <h1>园区能耗数据产品守护台</h1>
        </div>
        <div className="sub">
          SLO 与事故影响管理平台 · 演示账号 <span className="tag">ops / ops123</span> 或 <span className="tag">admin / admin123</span>
        </div>
        <div className="grid" style={{ gap: 10 }}>
          <label className="field">
            用户名
            <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          </label>
          <label className="field">
            密码
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
          </label>
          <button className="btn primary" type="submit" disabled={busy} style={{ justifyContent: 'center' }}>
            {busy ? '登录中…' : '登录'}
          </button>
          {error && <div className="err">{error}</div>}
        </div>
      </form>
    </div>
  )
}
