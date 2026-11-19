/**
 * Functional UI smoke test: mounts every page in jsdom against the running API and
 * asserts the key数据 actually rendered (not just that the app boots).
 */
import { JSDOM } from 'jsdom'

const BASE = process.env.API_BASE || 'http://127.0.0.1:8000'

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost:5173/',
  pretendToBeVisual: true,
})

const g = globalThis as any
g.window = dom.window
g.document = dom.window.document
Object.defineProperty(g, 'navigator', { value: dom.window.navigator, configurable: true })
g.HTMLElement = dom.window.HTMLElement
g.Element = dom.window.Element
g.Node = dom.window.Node
g.SVGElement = dom.window.SVGElement
g.localStorage = dom.window.localStorage
g.getComputedStyle = dom.window.getComputedStyle
g.requestAnimationFrame = (cb: any) => setTimeout(() => cb(Date.now()), 0)
g.cancelAnimationFrame = (id: any) => clearTimeout(id)

const realFetch = globalThis.fetch
g.fetch = (input: any, init?: any) => {
  const url = typeof input === 'string' ? input : input.url
  return realFetch(url.startsWith('http') ? url : BASE + url, init)
}
;(dom.window as any).fetch = g.fetch

const React = await import('react')
const { render, cleanup } = await import('@testing-library/react')
const { MemoryRouter } = await import('react-router-dom')
const { AppProvider } = await import('../src/lib/store')

// Make the smoke test self-contained: replay the acceptance narrative so every page
// has real incidents, impacts and versions to render.
{
  const res = await g.fetch(BASE + '/api/acceptance/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  if (!res.ok) {
    console.error('acceptance replay failed', res.status)
    process.exit(1)
  }
}

const pages = [
  ['health', (await import('../src/pages/Health')).default, ['产品健康', '观察窗口', '合格判定', 'M-ELE-B3-01', '核验缺口']],
  ['products', (await import('../src/pages/Products')).default, ['产品运行', '运行版本', '版本谱系', '核验日汇总']],
  ['registry', (await import('../src/pages/Registry')).default, ['来源登记', '采样承诺历史', 'M-ELE-B2-01', '允许迟到']],
  ['impact', (await import('../src/pages/Impact')).default, ['消费影响地图', '阈值敏感性', '物业调度大屏', '月度结算账单']],
  ['incidents', (await import('../src/pages/Incidents')).default, ['事故指挥', '事故列表', '处置动作', 'INC-']],
  ['simulator', (await import('../src/pages/Simulator')).default, ['模拟与验收', '审计时间线', '样本到达']],
  ['acceptance', (await import('../src/pages/Acceptance')).default, ['验收报告', '重放验收场景']],
  [
    'demand',
    (await import('../src/pages/Demand')).default,
    ['峰值需量证据', '楼栋合同状态', '尚未证实超限', '已证实超限', '风险区间', 'M-DMD-'],
  ],
] as const

const failures: string[] = []
let checks = 0

for (const [name, Page, needles] of pages) {
  const { container, unmount } = render(
    React.createElement(AppProvider, null, React.createElement(MemoryRouter, null, React.createElement(Page as any))),
  )
  await new Promise((r) => setTimeout(r, 2500))
  const text = container.textContent || ''
  if (process.env.DUMP) console.log(`--- ${name} ---`, text.slice(0, 900))
  for (const needle of needles as readonly string[]) {
    checks += 1
    if (!text.includes(needle)) failures.push(`${name}: missing "${needle}"`)
  }
  if (text.includes('无法加载') || text.length < 120) failures.push(`${name}: page looks empty (${text.length} chars)`)
  console.log(`${name.padEnd(12)} rendered ${String(text.length).padStart(6)} chars`)
  unmount()
  cleanup()
}

// Interaction: threshold preview on the impact page must change the verdict display.
{
  const Impact = (await import('../src/pages/Impact')).default
  const { container, unmount } = render(
    React.createElement(AppProvider, null, React.createElement(MemoryRouter, null, React.createElement(Impact as any))),
  )
  await new Promise((r) => setTimeout(r, 1600))
  const btn = Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes('预览判定变化'))
  if (!btn) failures.push('impact: preview button missing')
  else {
    btn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    await new Promise((r) => setTimeout(r, 1600))
    const text = container.textContent || ''
    checks += 1
    if (!text.includes('预览阈值')) failures.push('impact: preview result did not render')
  }
  unmount()
  cleanup()
}

await import('react-dom').then((m) => m.default)
dom.window.close()

if (failures.length) {
  console.error('\nUI SMOKE FAILURES:')
  for (const f of failures) console.error(' -', f)
  process.exit(1)
}
console.log(`\nUI smoke OK (${checks} assertions)`)
