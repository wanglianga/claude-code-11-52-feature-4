/** Interaction check: replay the peak-demand acceptance and assert the rendered
 *  evidence (contract verdicts, risk interval, peak-conclusion delta) is real data. */
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

const failures: string[] = []
let checks = 0
const need = (text: string, needle: string, where: string) => {
  checks += 1
  if (!text.includes(needle)) failures.push(`${where}: missing "${needle}"`)
}

// demand evidence page renders both verdicts and the range
{
  const Page = (await import('../src/pages/Demand')).default
  const { container, unmount } = render(
    React.createElement(AppProvider, null, React.createElement(MemoryRouter, null, React.createElement(Page as any))),
  )
  await new Promise((r) => setTimeout(r, 2500))
  const text = container.textContent || ''
  if (process.env.DUMP) console.log(text.slice(0, 1500))
  for (const needle of ['已证实未超限', '已证实超限', '尚未证实超限', '风险区间', '候选', '完整覆盖']) {
    need(text, needle, 'demand')
  }
  // switch to the integration route and confirm the B2 peak becomes confirmed
  const btn = Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes('区间积分'))
  if (!btn) failures.push('demand: integration toggle missing')
  else {
    btn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    await new Promise((r) => setTimeout(r, 1800))
    const after = container.textContent || ''
    checks += 1
    if (!after.includes('961.4')) failures.push('demand: integration peak 961.4 kW not rendered')
  }
  unmount()
  cleanup()
}

// acceptance page: replay the peak-demand narrative and render its checks
{
  const Page = (await import('../src/pages/Acceptance')).default
  const { container, unmount } = render(
    React.createElement(AppProvider, null, React.createElement(MemoryRouter, null, React.createElement(Page as any))),
  )
  await new Promise((r) => setTimeout(r, 1500))
  const btn = Array.from(container.querySelectorAll('button')).find((b) =>
    b.textContent?.includes('重放峰值需量验收'),
  )
  if (!btn) failures.push('acceptance: demand replay button missing')
  else {
    btn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    await new Promise((r) => setTimeout(r, 3000))
    const text = container.textContent || ''
    if (process.env.DUMP) console.log('--- acceptance ---', text.slice(0, 1400))
    for (const needle of ['峰值需量证据（7 步）', '累计表回退', '迟到高峰读数', '尚未证实超限', '峰值结论']) {
      need(text, needle, 'acceptance')
    }
  }
  unmount()
  cleanup()
}

await import('react-dom').then((m) => m.default)
dom.window.close()
if (failures.length) {
  console.error('\nDEMAND UI CHECK FAILURES:')
  for (const f of failures) console.error(' -', f)
  process.exit(1)
}
console.log(`\nDemand UI check OK (${checks} assertions)`)
