import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { chromium } from '../../backend/node_modules/playwright/index.mjs'

test('workspace menus are exclusive and hover preserves text metrics', async () => {
  const harness = (req, res, next) => {
    if (req.url !== '/__menu-test') return next()
    res.setHeader('Content-Type', 'text/html; charset=utf-8')
    res.end(`<html><body style="background:#101827;padding:200px 30px"><div id="app"></div>
      <script type="module">
        import {createApp,h} from '/node_modules/.vite/deps/vue.js';
        import Controls from '/src/components/RenderControls.vue';
        import Download from '/src/components/DownloadMenu.vue';
        createApp({render:()=>h('div',[h(Controls,{renderedCount:1,slidesCount:1,downloadVideoUrl:'/video',downloadSrtUrl:'/srt',downloadBundleUrl:'/zip'}),h(Download,{label:'其他下載',videoUrl:'/other'}),h('button',{id:'outside'},'外部')])}).mount('#app');
      </script></body></html>`)
  }
  const server = await createServer({
    configFile: false, root: fileURLToPath(new URL('..', import.meta.url)), plugins: [vue(), { name: 'test-harness', configureServer(server) { server.middlewares.use(harness) } }],
    server: { host: '127.0.0.1', port: 0 },
  })
  let browser
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 1000, height: 700 } })
    // Do not warm models or access real project data during the UI smoke test.
    await page.route('**/api/**', route => route.fulfill({ json: { runs: [], configured: false } }))
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/__menu-test`)
    page.setDefaultTimeout(5000)
    const merge = page.getByRole('button', { name: '合併匯出' })
    const download = page.getByRole('button', { name: '下載本頁' })
    await merge.click()
    const item = page.getByRole('menuitem', { name: '直接合併', exact: true })
    const style = () => item.evaluate(el => {
      const s = getComputedStyle(el)
      return { size: s.fontSize, weight: s.fontWeight, color: s.color, height: el.getBoundingClientRect().height, background: s.backgroundColor }
    })
    const before = await style()
    await item.hover()
    const after = await style()
    assert.notEqual(after.background, before.background)
    for (const key of ['size','weight','color','height']) assert.equal(after[key], before[key])
    await download.click()
    assert.equal(await page.getByRole('menu').count(), 1)
    assert.equal(await item.count(), 0)
    await merge.click()
    assert.equal(await page.getByRole('menu').count(), 1)
    await page.getByRole('menuitem', { name: '自動轉場' }).click()
    assert.equal(await page.getByRole('menu').count(), 0)
    await download.click()
    await page.keyboard.press('Escape')
    assert.equal(await page.getByRole('menu').count(), 0)
    await download.click()
    await page.getByRole('button', { name: '其他下載' }).click()
    assert.equal(await page.getByRole('menu').count(), 1)
    await page.locator('#outside').click()
    assert.equal(await page.getByRole('menu').count(), 0)
    assert.deepEqual(errors, [])
    // Also mount the real workspace: immediate watchers must not access
    // const functions before setup has initialized them.
    await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/`)
    await page.getByText('請上傳一份PDF檔案', { exact: false }).waitFor()
    assert.deepEqual(errors, [])
  } finally {
    await browser?.close()
    await server.close()
  }
})
