// End-to-end test: browsing the Intelligence list without losing your place.
//
// Drives the REAL production build in headless Chrome against a synthetic,
// deterministic /api (no production data, no model calls). The mock COUNTS
// /intel/search hits, because the defect this guards against is not visual:
// coming back from a call used to cost a whole new AI-judged pass.
//
//   cd frontend
//   npx vite build --outDir /tmp/lescall-e2e-dist --emptyOutDir
//   node e2e/intel_browse.e2e.mjs --dist /tmp/lescall-e2e-dist
//
// Puppeteer is not a dependency of this package (it would pull a ~300 MB
// browser into the SPA image build). Point PUPPETEER_FROM at any directory
// whose node_modules has it; the default is the product-video workspace.
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const arg = (name, dflt) => {
  const i = process.argv.indexOf(name)
  return i > 0 ? process.argv[i + 1] : dflt
}
const DIST = path.resolve(arg('--dist', '/tmp/lescall-e2e-dist'))
const PORT = Number(arg('--port', 4391))
const FROM = process.env.PUPPETEER_FROM || '/home/angelo/lescall-video/capture'
const puppeteer = createRequire(path.join(FROM, 'package.json'))('puppeteer')

if (!fs.existsSync(path.join(DIST, 'index.html'))) {
  console.error(`no build at ${DIST} — run: npx vite build --outDir ${DIST} --emptyOutDir`)
  process.exit(2)
}

/* ------------------------------------------------------------------ data */

const QUERY = 'people willing to relocate to vapi with prior AI experience'
const LIST_TOTAL = 120          // structured list: 3 server pages of 50
const AI_TOTAL = 60             // AI filter: 2 client pages of 50
const key = (n) => `k-${String(n).padStart(4, '0')}`

const row = (n, extra = {}) => ({
  call_id: n, call_key: key(n), phone: `+91 55501 ${String(n).padStart(5, '0')}`,
  direction: 'outbound', agent_slug: n % 2 ? 'agent-a' : 'agent-b',
  started_at: new Date(Date.UTC(2026, 8, 1, 10, 0, 0) - n * 3600_000).toISOString(),
  duration_s: 60 + n, outcome: 'completed', contact_id: null, language: 'en',
  contact_name: `Sample Person ${n}`, summary: `Synthetic summary ${n}`,
  customer_intent: 'job enquiry', sentiment: n % 3 === 0 ? 'negative' : 'positive',
  urgency: 'low', lead_score: 50, opportunity_score: 50, risk_score: 10,
  compliance_score: 90, confidence: 0.9, low_confidence: 0, tags: [],
  processed_at: null, model: 'mock', followup_id: null, followup_decision: 'no',
  followup_status: null, followup_at: null, followup_priority: null, ...extra,
})
const ALL = Array.from({ length: LIST_TOTAL }, (_, i) => row(i + 1))
// The AI result is the ODD calls, in a different order than the list — so a
// pager that walked the structured list by mistake would be caught.
const AI_ROWS = ALL.filter((r) => r.call_id % 2 === 1).slice(0, AI_TOTAL).reverse()
  .map((r) => ({ ...r, match: 'verified', match_confidence: 0.9,
    match_evidence: `I can relocate to Vapi (${r.call_key})`, match_reason: 'all checks true' }))

const detail = (k) => {
  const r = ALL.find((x) => x.call_key === k)
  if (!r) return null
  return {
    call: r, audio_url: null,
    intelligence: { summary: `Detail for ${k}`, confidence: 0.9, low_confidence: 0,
      lead_score: 50, opportunity_score: 50, risk_score: 10, compliance_score: 90,
      sentiment: 'positive', urgency: 'low', conversion_probability: 0.5, tags: [],
      buying_signals: [], objections: [], compliance_issues: [], topics: [],
      pain_points: [], product_interest: [], competitor_mentions: [],
      budget_discussion: [], timeline_discussion: [], decision_makers: [],
      risk_indicators: [], escalations: [], agent_performance: {}, key_moments: [],
      recommendations: {} },
    transcript: [{ timestamp: '00:01', speaker: 'Agent', text: `Hello from ${k}` }],
    commitments: [], action_items: [], followup: null, memory: null,
  }
}

/* ---------------------------------------------------------------- server */

const hits = { search: [], listPages: [] }
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json' }

function send(res, code, body, type = 'application/json') {
  res.writeHead(code, { 'content-type': type, 'cache-control': 'no-store' })
  res.end(typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body))
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x')
  const p = url.pathname
  if (p.startsWith('/api/')) {
    const api = p.slice(4)
    const q = url.searchParams
    if (api.endsWith('/stream')) {                       // SSE: hold open, say nothing
      res.writeHead(200, { 'content-type': 'text/event-stream' }); return
    }
    if (api === '/intel/search') {
      hits.search.push({ q: q.get('q'), agent: q.get('agent') || '', limit: q.get('limit') })
      const agent = q.get('agent') || ''
      const items = agent ? AI_ROWS.filter((r) => agent.split(',').includes(r.agent_slug)) : AI_ROWS
      return void setTimeout(() => send(res, 200, {
        items, degraded: false, query: q.get('q'), verified: true,
        applied: { agent, query: q.get('q') },
        verification: { enabled: true, model: 'mock-judge', candidates: 100, examined: 100,
          cached: 0, matched: items.length, returned: items.length, pool_full: false,
          truncated: false, identifier_lookup: false,
          criterion: { restated: 'synthetic', requirements: ['req one'], excludes: [], planned: true } },
      }), 250)
    }
    if (api === '/intel/calls') {
      const page = Number(q.get('page') || 1), limit = Number(q.get('limit') || 50)
      let rows = ALL
      if (q.get('sentiment')) rows = rows.filter((r) => r.sentiment === q.get('sentiment'))
      hits.listPages.push({ page, sentiment: q.get('sentiment') || '' })
      return send(res, 200, { items: rows.slice((page - 1) * limit, page * limit),
        total: rows.length, page, limit })
    }
    if (api === '/intel/calls/ids') return send(res, 200, { call_ids: ALL.map((r) => r.call_id), count: ALL.length, capped: false, max: 2000 })
    const m = api.match(/^\/intel\/calls\/([^/]+)$/)
    if (m) { const d = detail(m[1]); return d ? send(res, 200, d) : send(res, 404, { detail: 'not found' }) }
    if (api === '/intel/analytics') return send(res, 200, { summary: { analyzed: LIST_TOTAL, avg_lead: 50, hot_leads: 0, positive: 80, neutral: 0, negative: 40 }, agent_options: ['agent-a', 'agent-b'], topics: [], objections: [], products: [], trends: [], followup_decisions: [] })
    if (api === '/intel/status') return send(res, 200, { queue: {}, coverage: { calls: LIST_TOTAL, analyzed: LIST_TOTAL, embedded: LIST_TOTAL } })
    if (api === '/intel/notifications') return send(res, 200, { items: [], unread: 0 })
    if (api === '/auth/me') return send(res, 200, { mode: 'multi-tenant', login_required: false, email: 'tester@example.test', name: 'E2E Tester', picture: '' })
    if (api === '/health') return send(res, 200, { services: {}, ports: {}, overall: 'healthy', billing: { enforcement: false } })
    return send(res, 200, req.method === 'GET' ? {} : { ok: true })
  }
  let file = path.join(DIST, decodeURIComponent(p))
  if (!file.startsWith(DIST)) return send(res, 403, 'no', 'text/plain')
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST, 'index.html')
  send(res, 200, fs.readFileSync(file), MIME[path.extname(file)] || 'application/octet-stream')
})

/* --------------------------------------------------------------- harness */

const ORIGIN = `http://127.0.0.1:${PORT}`
const results = []
let page

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(fn, what, ms = 8000) {
  const t0 = Date.now()
  let last
  while (Date.now() - t0 < ms) {
    try { last = await fn(); if (last) return last } catch (e) { last = e }
    await sleep(60)
  }
  throw new Error(`timed out waiting for: ${what} (last: ${String(last)})`)
}
const text = (sel) => page.$eval(sel, (el) => el.textContent.trim()).catch(() => '')
const body = () => page.evaluate(() => document.body.innerText)
const urlNow = () => new URL(page.url())
const pagerText = () => text('[role="group"][aria-label^="Step through"] span[aria-live]')
const back = () => page.evaluate(() => history.back())
const currentRowKey = () => page.evaluate(() => {
  const tr = document.querySelector('tr[aria-current="true"]')
  return tr ? tr.querySelector('[aria-label^="Select call "]')?.getAttribute('aria-label').replace('Select call ', '') : null
})
const clickRow = (k) => page.evaluate((k) => {
  const cb = document.querySelector(`[aria-label="Select call ${k}"]`)
  cb.closest('tr').querySelector('td:nth-child(3)').click()
}, k)
const clickLabel = (label) => page.evaluate((label) => {
  const el = [...document.querySelectorAll('button')].find((b) => (b.getAttribute('aria-label') || '').startsWith(label))
  if (!el) throw new Error(`no button labelled ${label}`)
  el.click()
}, label)

function eq(actual, expected, what) {
  if (actual !== expected) throw new Error(`${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
}
function ok(cond, what) { if (!cond) throw new Error(what) }

async function test(name, fn) {
  try { await fn(); results.push({ name, pass: true }); console.log(`  PASS  ${name}`) }
  catch (e) { results.push({ name, pass: false, err: e.message }); console.log(`  FAIL  ${name}\n        ${e.message}`) }
}

async function runFilter() {
  await page.goto(`${ORIGIN}/intelligence`, { waitUntil: 'networkidle2' })
  await page.waitForSelector('input[aria-label="Semantic call search"]')
  await page.type('input[aria-label="Semantic call search"]', QUERY)
  await page.keyboard.press('Enter')
  await until(async () => (await body()).includes(`${AI_TOTAL} call(s) matching`), 'AI filter result')
}

/* ----------------------------------------------------------------- tests */

async function main() {
  await new Promise((r) => server.listen(PORT, '127.0.0.1', r))
  const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] })
  page = await browser.newPage()
  await page.setViewport({ width: 1440, height: 900 })
  const pageErrors = []
  page.on('pageerror', (e) => pageErrors.push(e.message))

  console.log('\nAI filter persistence')
  await test('running a filter puts it in the URL', async () => {
    await runFilter()
    eq(urlNow().searchParams.get('ai'), QUERY, '?ai=')
    eq(hits.search.length, 1, 'search calls')
    eq(hits.search[0].limit, '300', 'limit sent')
  })

  await test('open a call, press Back: filter still active, NOT re-run', async () => {
    await clickRow(AI_ROWS[0].call_key)
    await until(async () => (await pagerText()) === `1 of ${AI_TOTAL}`, 'pager on call page')
    await back()
    await until(async () => (await body()).includes(`${AI_TOTAL} call(s) matching`), 'filter after Back')
    eq(urlNow().searchParams.get('ai'), QUERY, '?ai= after Back')
    eq(await page.$eval('input[aria-label="Semantic call search"]', (el) => el.value), QUERY, 'search box text')
    ok((await body()).includes('AI-verified'), 'verification panel restored')
    eq(hits.search.length, 1, 'search calls after Back (a re-run here is the bug)')
  })

  await test('the row just opened is marked on return', async () => {
    eq(await currentRowKey(), AI_ROWS[0].call_key, 'aria-current row')
    ok((await body()).includes('last opened'), '"last opened" label')
  })

  console.log('\nPrevious / Next on the call page')
  await test('pager walks the AI RESULT order, not the default list', async () => {
    await clickRow(AI_ROWS[0].call_key)
    await until(async () => (await pagerText()) === `1 of ${AI_TOTAL}`, 'pager 1')
    ok(await page.$eval('button[aria-label^="Previous call"]', (b) => b.disabled), 'Previous disabled on first')
    await clickLabel('Next call')
    await until(() => page.url().endsWith(`/intelligence/${AI_ROWS[1].call_key}`), 'url of 2nd AI row')
    eq(await pagerText(), `2 of ${AI_TOTAL}`, 'pager text')
    await until(async () => (await body()).includes(`Detail for ${AI_ROWS[1].call_key}`), 'detail of 2nd call rendered')
  })

  await test('→ / ← keys step; typing in the transcript search does not', async () => {
    await page.keyboard.press('ArrowRight')
    await until(() => page.url().endsWith(AI_ROWS[2].call_key), '→ to 3rd')
    await page.keyboard.press('ArrowLeft')
    await until(() => page.url().endsWith(AI_ROWS[1].call_key), '← to 2nd')
    await page.waitForSelector('input[aria-label="Search transcript"]')
    await page.click('input[aria-label="Search transcript"]')
    await page.keyboard.type('he')
    await page.keyboard.press('ArrowRight')
    await sleep(300)
    ok(page.url().endsWith(AI_ROWS[1].call_key), 'arrow key inside an input must not change call')
    await page.evaluate(() => document.activeElement.blur())
  })

  await test('breadcrumb points at the filtered list, not bare /intelligence', async () => {
    const href = await page.$eval('nav[aria-label="Breadcrumb"] a', (a) => a.getAttribute('href'))
    ok(href.includes('ai='), `crumb href keeps the filter (got ${href})`)
  })

  await test('step onto page 2 of the result, ONE Back returns to the list on page 2', async () => {
    // from #2 to #51 = 49 steps; drive the hook directly through the button
    for (let i = 2; i <= 50; i++) {
      await clickLabel('Next call')
      await until(() => page.url().endsWith(AI_ROWS[i].call_key), `step to ${i + 1}`)
    }
    eq(await pagerText(), `51 of ${AI_TOTAL}`, 'pager at 51')
    const crumb = await page.$eval('nav[aria-label="Breadcrumb"] a', (a) => a.getAttribute('href'))
    ok(crumb.includes('page=2'), `crumb names page 2 (got ${crumb})`)
    await back()
    await until(async () => (await body()).includes(`${AI_TOTAL} call(s) matching`), 'list after one Back')
    await until(async () => (await currentRowKey()) === AI_ROWS[50].call_key, 'row 51 marked + visible (page 2)')
    eq(urlNow().searchParams.get('page'), '2', '?page=')
    eq(hits.search.length, 1, 'search calls (still one)')
  })

  console.log('\nSurvives more than Back')
  await test('hard refresh keeps the filter without re-running it', async () => {
    await page.reload({ waitUntil: 'networkidle2' })
    await until(async () => (await body()).includes(`${AI_TOTAL} call(s) matching`), 'filter after reload')
    eq(hits.search.length, 1, 'search calls after reload')
    eq(urlNow().searchParams.get('page'), '2', 'a refresh must not move the page')
  })

  await test('ticked rows survive opening a call', async () => {
    await page.goto(`${ORIGIN}/intelligence?ai=${encodeURIComponent(QUERY)}`, { waitUntil: 'networkidle2' })
    await until(async () => (await body()).includes(`${AI_TOTAL} call(s) matching`), 'filter')
    for (const i of [3, 4]) await page.click(`[aria-label="Select call ${AI_ROWS[i].call_key}"]`)
    await until(async () => (await body()).includes('Export to Excel (2)'), '2 ticked')
    await clickRow(AI_ROWS[5].call_key)
    await until(async () => (await pagerText()) === `6 of ${AI_TOTAL}`, 'call page')
    await back()
    await until(async () => (await body()).includes('Export to Excel (2)'), 'ticks restored after Back')
    eq(hits.search.length, 1, 'search calls')
  })

  await test('sidebar-style fresh visit offers Resume; one click restores, no re-run', async () => {
    await page.goto(`${ORIGIN}/intelligence`, { waitUntil: 'networkidle2' })
    await until(async () => (await body()).includes('Resume “'), 'resume chip')
    ok(!(await body()).includes('call(s) matching'), 'a bare visit must NOT auto-apply the filter')
    await page.evaluate(() => [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Resume “')).click())
    await until(async () => (await body()).includes(`${AI_TOTAL} call(s) matching`), 'filter resumed')
    eq(hits.search.length, 1, 'search calls')
  })

  await test('a pasted link in a clean browser runs the filter exactly once', async () => {
    const ctx = await browser.createBrowserContext()
    const p2 = await ctx.newPage()
    const before = hits.search.length
    await p2.goto(`${ORIGIN}/intelligence?ai=${encodeURIComponent(QUERY)}`, { waitUntil: 'networkidle2' })
    const t0 = Date.now()
    while (Date.now() - t0 < 8000 && !(await p2.evaluate(() => document.body.innerText)).includes(`${AI_TOTAL} call(s) matching`)) await sleep(60)
    ok((await p2.evaluate(() => document.body.innerText)).includes(`${AI_TOTAL} call(s) matching`), 'rows shown from a bare link')
    eq(hits.search.length - before, 1, 'search calls from the link')
    await ctx.close()
  })

  await test('changing the agent scope under an active filter re-judges for that scope', async () => {
    const before = hits.search.length
    await page.goto(`${ORIGIN}/intelligence?ai=${encodeURIComponent(QUERY)}&agent=agent-a`, { waitUntil: 'networkidle2' })
    await until(async () => /\d+ call\(s\) matching/.test(await body()), 'scoped result')
    eq(hits.search.length - before, 1, 'one new pass')
    eq(hits.search.at(-1).agent, 'agent-a', 'agent sent to the server')
  })

  await test('Clear AI filter removes it from the URL and offers no Resume', async () => {
    await page.evaluate(() => [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Clear AI filter')).click())
    await until(() => !urlNow().searchParams.get('ai'), '?ai= removed')
    await until(async () => (await body()).includes(`${LIST_TOTAL} results`) || (await body()).includes('results'), 'plain list')
    ok(!(await body()).includes('Resume “'), 'no resume chip after an explicit clear')
  })

  console.log('\nStructured (server-paged) list')
  await test('Next walks past the end of server page 1 into page 2', async () => {
    await page.goto(`${ORIGIN}/intelligence?days=0`, { waitUntil: 'networkidle2' })
    await page.waitForSelector(`[aria-label="Select call ${key(50)}"]`)
    await clickRow(key(50))
    await until(async () => (await pagerText()) === `50 of ${LIST_TOTAL}`, 'pager 50 of 120')
    const pagesBefore = hits.listPages.filter((h) => h.page === 2).length
    await clickLabel('Next call')
    await until(() => page.url().endsWith(`/intelligence/${key(51)}`), 'stepped onto page-2 call')
    eq(await pagerText(), `51 of ${LIST_TOTAL}`, 'pager text')
    ok(hits.listPages.filter((h) => h.page === 2).length > pagesBefore, 'page 2 fetched on demand')
    await back()
    await until(async () => (await currentRowKey()) === key(51), 'list on page 2 with row 51 marked')
    eq(urlNow().searchParams.get('page'), '2', '?page=')
  })

  await test('a structured filter rides along when paging from the call page', async () => {
    await page.goto(`${ORIGIN}/intelligence?days=0&sentiment=negative`, { waitUntil: 'networkidle2' })
    const neg = ALL.filter((r) => r.sentiment === 'negative')
    await page.waitForSelector(`[aria-label="Select call ${neg[0].call_key}"]`)
    await clickRow(neg[0].call_key)
    await until(async () => (await pagerText()) === `1 of ${neg.length}`, 'pager counts the FILTERED total')
    await clickLabel('Next call')
    await until(() => page.url().endsWith(neg[1].call_key), 'next = next NEGATIVE call')
    const crumb = await page.$eval('nav[aria-label="Breadcrumb"] a', (a) => a.getAttribute('href'))
    ok(crumb.includes('sentiment=negative'), `crumb keeps the structured filter (got ${crumb})`)
  })

  await test('a call opened by direct link shows no pager', async () => {
    const ctx = await browser.createBrowserContext()
    const p2 = await ctx.newPage()
    await p2.goto(`${ORIGIN}/intelligence/${key(7)}`, { waitUntil: 'networkidle2' })
    await p2.waitForSelector('nav[aria-label="Breadcrumb"]')
    eq(await p2.$('[role="group"][aria-label^="Step through"]'), null, 'pager element')
    await ctx.close()
  })

  await test('no uncaught page errors during the whole run', async () => {
    eq(pageErrors.length, 0, `page errors: ${pageErrors.join(' | ')}`)
  })

  await browser.close()
  server.close()
  const failed = results.filter((r) => !r.pass)
  console.log(`\n${results.length - failed.length}/${results.length} passed`)
  process.exit(failed.length ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(1) })
