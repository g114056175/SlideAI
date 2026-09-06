import test from 'node:test'
import assert from 'node:assert/strict'
import { createRenderer, ref, nextTick } from 'vue'
import { useRenderQueue } from '../src/composables/useRenderQueue.js'

const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {},
})
function setup() {
  const state = {
    slides: ref([{ scriptText: 'script' }]), selectedSlide: ref({ scriptText: 'script' }), currentRunId: ref('run-a'),
    enableSubtitleHighlight: ref(false),
    rendering: ref(false), renderingAll: ref(false), renderMessage: ref(''),
    selectedSlideIndex: ref(0), renderedPageVideos: ref({}), selectedVariantIds: ref({}),
    globalSettings: ref({ tts: { voice: 'voice', speed: 1 }, subtitle: {} }),
    referenceText: ref('reference'), cloneAudioFile: ref(null), selectedVoiceKey: ref('voice'),
    getApiEndpoint: path => path, refreshRunManifest: async () => {},
  }
  let queue
  const app = renderer.createApp({ setup() { queue = useRenderQueue(state); return () => null } })
  app.mount({})
  return { state, queue, app }
}
const response = (data, status = 200) => new Response(status === 204 ? null : JSON.stringify(data), { status })
const job = status => ({ job_id: 'job-a', status, payload: { page_indexes: [0] }, pages: {} })

for (const accept of [true, false]) {
  test(`interrupted recovery ${accept ? 'accepts' : 'declines'} exactly once`, async t => {
    let prompts = 0
    const calls = []
    const originalFetch = globalThis.fetch, originalWindow = globalThis.window
    globalThis.window = { confirm() { prompts++; return accept } }
    globalThis.fetch = async (url, options = {}) => {
      calls.push([url, options.method || 'GET'])
      if (url.endsWith('/jobs-current')) return prompts ? response(null, 204) : response(job('interrupted'))
      return response(job(url.endsWith('/resume') ? 'queued' : url.endsWith('/cancel') ? 'cancelled' : 'completed'))
    }
    const { queue, app } = setup()
    t.after(() => { app.unmount(); globalThis.fetch = originalFetch; globalThis.window = originalWindow })
    await queue.reattachActiveBatchJob()
    await queue.reattachActiveBatchJob()
    assert.equal(prompts, 1)
    assert.ok(calls.some(([url, method]) => url.endsWith(accept ? '/resume' : '/cancel') && method === 'POST'))
    assert.ok(calls.every(([url]) => url.includes('/run-a/')))
  })
}

test('running job reattaches without asking to restart', async t => {
  const originalFetch = globalThis.fetch, originalWindow = globalThis.window
  globalThis.window = { confirm() { assert.fail('running job must not prompt') } }
  globalThis.fetch = async url => response(job(url.endsWith('/jobs-current') ? 'running' : 'completed'))
  const { queue, app } = setup()
  t.after(() => { app.unmount(); globalThis.fetch = originalFetch; globalThis.window = originalWindow })
  await queue.reattachActiveBatchJob()
})

test('late response from previous project does not prompt or post', async t => {
  const originalFetch = globalThis.fetch, originalWindow = globalThis.window
  let finish
  globalThis.window = { confirm() { assert.fail('stale project must not prompt') } }
  globalThis.fetch = () => new Promise(resolve => { finish = resolve })
  const { state, queue, app } = setup()
  t.after(() => { app.unmount(); globalThis.fetch = originalFetch; globalThis.window = originalWindow })
  const pending = queue.reattachActiveBatchJob()
  state.currentRunId.value = 'run-b'
  await nextTick()
  finish(response(job('interrupted')))
  assert.equal(await pending, false)
  assert.equal(state.renderMessage.value, '')
})

test('unmounted workspace does not prompt on a late response', async t => {
  const originalFetch = globalThis.fetch, originalWindow = globalThis.window
  let finish
  globalThis.window = { confirm() { assert.fail('unmounted workspace must not prompt') } }
  globalThis.fetch = () => new Promise(resolve => { finish = resolve })
  const { queue, app } = setup()
  t.after(() => { globalThis.fetch = originalFetch; globalThis.window = originalWindow })
  const pending = queue.reattachActiveBatchJob()
  app.unmount()
  finish(response(job('interrupted')))
  assert.equal(await pending, false)
})

test('backend interruption while attached prompts and resumes', async t => {
  const originalFetch = globalThis.fetch, originalWindow = globalThis.window
  let resumed = false, prompts = 0
  globalThis.window = { confirm() { prompts++; return true } }
  globalThis.fetch = async url => {
    if (url.endsWith('/jobs-current')) return response(job('running'))
    if (url.endsWith('/resume')) { resumed = true; return response(job('queued')) }
    return response(job(resumed ? 'completed' : 'interrupted'))
  }
  const { queue, app } = setup()
  t.after(() => { app.unmount(); globalThis.fetch = originalFetch; globalThis.window = originalWindow })
  await queue.reattachActiveBatchJob()
  assert.equal(prompts, 1)
  assert.equal(resumed, true)
})

for (const runId of [null, '', '   ']) {
  test(`project operations reject missing run_id (${JSON.stringify(runId)}) without requests`, async t => {
    const originalFetch = globalThis.fetch, originalWindow = globalThis.window
    globalThis.fetch = async () => assert.fail('missing run must never send a request')
    globalThis.window = { confirm() { assert.fail('missing run must be rejected before merge confirmation') } }
    const { state, queue, app } = setup()
    t.after(() => { app.unmount(); globalThis.fetch = originalFetch; globalThis.window = originalWindow })
    state.currentRunId.value = runId
    await nextTick()
    for (const operation of [
      () => queue.renderCurrentPage(), () => queue.renderAllPages(),
      () => queue.mergeAndDownloadRenderedVideos(),
      () => queue.regenerateTtsChunk({ variantId: 'variant-a', chunkIndex: 0, text: 'replacement' }),
    ]) {
      state.renderMessage.value = ''
      await operation()
      assert.match(state.renderMessage.value, /run_id/)
      assert.equal(state.rendering.value, false)
      assert.equal(state.renderingAll.value, false)
      assert.deepEqual(queue.singleRenderQueue.value, [])
    }
  })
}

test('single-page rendering sends persisted identifiers without fetching or reuploading media', async t => {
  const originalFetch = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push(url)
    assert.equal(options.body.has('audio_file'), false)
    assert.equal(options.body.has('slide_image'), false)
    if (url.endsWith('/tts')) return response({ variant_id: 'variant-a' })
    if (url.endsWith('/align')) {
      assert.equal(options.body.get('variant_id'), 'variant-a')
      return response({ variant_id: 'variant-a', segments: [{ start: 0, end: 1, text: 'script' }] })
    }
    assert.equal(url, '/api/video-abstract/render-subtitle-ass-video')
    assert.equal(options.body.get('run_id'), 'run-a')
    assert.equal(options.body.get('page_index'), '0')
    assert.equal(options.body.get('variant_id'), 'variant-a')
    return new Response(new Blob(['video']))
  }
  const { state, queue, app } = setup()
  state.slides.value[0].thumbnailUrl = '/must-not-fetch.png'
  t.after(() => {
    app.unmount(); globalThis.fetch = originalFetch
    Object.values(state.renderedPageVideos.value).forEach(url => URL.revokeObjectURL(url))
  })
  await queue.renderCurrentPage()
  assert.deepEqual(calls, ['/api/video-runs/run-a/pages/0/tts', '/api/video-runs/run-a/pages/0/align', '/api/video-abstract/render-subtitle-ass-video'])
  assert.match(state.renderMessage.value, /渲染完成/)
})
