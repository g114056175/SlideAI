import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'

const component = readFileSync(new URL('../src/components/VideoAbstractLab.vue', import.meta.url), 'utf8')
const handler = component.slice(component.indexOf('const handleUpload = async () => {'), component.indexOf('\nconst fileToBase64'))
const ref = value => ({ value })
function setup(payload) {
  const context = {
    FormData,
    pdfFile: ref(new Blob(['pdf'])), resetMessages() {}, uploading: ref(false), uploadPhase: ref(''),
    subtitleSource: ref('zh'), contentLanguage: ref('zh'), globalSettings: ref({ tts: { voice: 'voice' } }),
    userProvidedScript: ref(''), getApiEndpoint: path => path, API_ENDPOINTS: { VIDEO_ABSTRACT: '/api/video-abstract' },
    fetch: async () => new Response(JSON.stringify(payload)),
    pdfId: ref(null), currentRunId: ref(null), resetImageSession() {}, imageSession: 0,
    runManifest: ref(null), selectedVariantIds: ref({}), suppressScriptSave: ref(false),
    slides: ref([]), createSlideModels: texts => texts.map(scriptText => ({ scriptText })),
    selectedSlideIndex: ref(0), renderedPageVideos: ref({}), refreshSidebarRuns() {},
    refreshRunManifest: async () => {}, nextTick: async () => {},
    generateScriptsForPages: () => assert.fail('basic deployment must skip AI generation'),
    prepareInitialRunImages: async runId => assert.equal(runId, 'run-basic'),
    preloadRemainingRunImages: runId => assert.equal(runId, 'run-basic'),
    stage: ref('upload'), activeTab: ref(''), statusMessage: ref(''), errorMessage: ref(''),
  }
  return { context, upload: runInNewContext(`${handler}\nhandleUpload`, context) }
}

for (const runId of [undefined, '', '   ']) {
  test(`upload without a valid run_id stays in upload screen (${JSON.stringify(runId)})`, async () => {
    const { context, upload } = setup({ pdf_id: 'legacy-pdf', run_id: runId, texts: ['script'] })
    await upload()
    assert.equal(context.stage.value, 'upload')
    assert.equal(context.currentRunId.value, null)
    assert.equal(context.slides.value.length, 0)
    assert.match(context.errorMessage.value, /run_id/)
    assert.equal(context.uploading.value, false)
  })
}

test('basic deployment upload opens canonical run while skipping model generation', async () => {
  const { context, upload } = setup({ run_id: 'run-basic', texts: [''], model_services_skipped: true })
  await upload()
  assert.equal(context.currentRunId.value, 'run-basic')
  assert.equal(context.stage.value, 'workspace')
  assert.equal(context.slides.value.length, 1)
  assert.equal(context.errorMessage.value, '')
  assert.match(context.statusMessage.value, /基本前後端模式/)
})
