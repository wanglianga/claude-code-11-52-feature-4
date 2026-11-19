export type JSONObj = Record<string, any>

const TOKEN_KEY = 'park-slo-token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      /* keep status text */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

export const api = {
  get: <T = any>(path: string) => request<T>(path),
  post: <T = any>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
}

export function fmtTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function fmtClock(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function fmtWindow(start?: string, end?: string): string {
  if (!start || !end) return '—'
  return `${fmtTime(start)} → ${fmtClock(end)}`
}

export const STATUS_LABEL: Record<string, string> = {
  healthy: '健康',
  degraded: '迟到降级',
  breached: '违约',
  pending: '待定',
  partial: '部分',
  ok: '正常',
  restricted: '受限',
  unavailable: '不可用',
  waiting: '等待',
  open: '打开',
  investigating: '处理中',
  recovering: '恢复确认',
  reopened: '重开',
  closed: '关闭',
  qualified: '合格',
  provisional: '暂定',
  failed: '不合格',
  current: '当前',
  lagging: '滞后',
  stale: '过期',
  late_arrival: '迟到',
  none: '无',
  degraded_sev: '降级',
  blocked: '阻断',
  pending_sev: '待定',
}

export function statusTone(status?: string): 'good' | 'warn' | 'bad' | 'info' | 'muted' {
  switch (status) {
    case 'healthy':
    case 'ok':
    case 'qualified':
    case 'closed':
    case 'current':
    case 'none':
      return 'good'
    case 'degraded':
    case 'restricted':
    case 'partial':
    case 'provisional':
    case 'recovering':
    case 'reopened':
    case 'late_arrival':
    case 'lagging':
    case 'degraded_sev':
      return 'warn'
    case 'breached':
    case 'unavailable':
    case 'failed':
    case 'blocked':
    case 'stale':
      return 'bad'
    case 'pending':
    case 'waiting':
    case 'investigating':
    case 'open':
    case 'pending_sev':
      return 'info'
    default:
      return 'muted'
  }
}

export const SLOT_LABEL: Record<string, string> = {
  covered: '已覆盖',
  missing: '缺失',
  pending: '待定',
  maintenance: '维护扣除',
  verification: '核验缺口',
  beyond_cutoff: '未到截止',
}
