<template>
  <AppShell
    :active-run-id="currentRunId || ''"
    @new-project="backToUpload"
    @select-project="handleShellProjectSelect"
    @project-deleted="handleShellProjectDeleted"
  >
    <div class="lab-page">
      <!-- Stage A: Keep the familiar upload UI first -->
      <div v-if="stage === 'upload' && !openingProject" class="upload-stage">
        <div class="card upload-card shadow-sm border-0">
          <div class="card-body p-4 p-md-5">
            <h2 class="text-center mb-3 title">AI語音簡報</h2>
            <p class="text-center subtitle mb-4">請上傳一份PDF檔案，AI會自動生成語音簡報。</p>

            <div
              class="upload-area w-100 mb-3"
              :class="{ 'drag-over': isDragOver }"
              @click="() => $refs.fileInput.click()"
            >
              <input ref="fileInput" type="file" accept="application/pdf" class="d-none" @change="onFileChange" />
              <!-- 透明覆蓋層：統一接管所有 drag 事件，解決子元素裫罪 drag event 問題 -->
              <div
                class="upload-drag-overlay"
                @dragover.prevent="onDragOver"
                @dragleave="onDragLeave"
                @drop.prevent="onDrop"
              ></div>
              <div class="text-center py-4" style="pointer-events: none;">
                <div class="upload-icon">📄</div>
                <div class="upload-text">{{ pdfFile ? pdfFile.name : '點擊或拖曳PDF檔案到這裡' }}</div>
                <div class="upload-tip">僅支援 PDF，最大 20MB</div>
              </div>
            </div>

            <div class="row g-3 align-items-end">
              <div class="col-12">
                <label class="form-label">生成字幕</label>
                <select v-model="subtitleSource" class="form-select dark-input">
                  <option value="zh">中文</option>
                  <option value="en">英文</option>
                  <option value="none">無</option>
                  <option value="user_input">使用者輸入</option>
                </select>
              </div>
            </div>

            <div v-if="subtitleSource === 'user_input'" class="mt-3">
              <label class="form-label">使用者輸入講稿</label>
              <textarea
                v-model="userProvidedScript"
                class="form-control dark-input user-script-box"
                rows="8"
                placeholder="建議使用固定格式：#PAGE_001# ... #END_PAGE_001#、#PAGE_002# ... #END_PAGE_002#。也支援「第1頁:」「Page 1:」「Slide 1:」與空行分段。"
              />
            </div>

            <button class="btn btn-primary w-100 mt-4" :disabled="!pdfFile || uploading" @click="handleUpload">
              <span v-if="uploading" class="spinner-border spinner-border-sm me-2" />
              {{ uploading ? uploadButtonText : '確認' }}
            </button>

            <div v-if="statusMessage" class="alert alert-success mt-3 mb-0 py-2">{{ statusMessage }}</div>
            <div v-if="errorMessage" class="alert alert-danger mt-3 mb-0 py-2">{{ errorMessage }}</div>
          </div>
        </div>
      </div>

      <!-- Stage B: Workflow after upload -->
      <div v-else-if="stage === 'workspace' && !openingProject" class="workspace-stage">
        <div class="workflow-card">
          <div class="card-body">
            <div class="tabs-row">
              <button class="btn tab-btn" :class="{ active: activeTab === 'script' }" @click="activeTab = 'script'">📝 講稿修改</button>
              <button class="btn tab-btn" :class="{ active: activeTab === 'tts' }" @click="activeTab = 'tts'">🎙️ 語音設定</button>
              <button class="btn tab-btn" :class="{ active: activeTab === 'subtitle' }" @click="activeTab = 'subtitle'">🔤 字幕設定</button>
              <button class="btn tab-btn preview-tab" :class="{ active: activeTab === 'preview' }" @click="activeTab = 'preview'">🎬 預覽輸出</button>
            </div>

            <div class="workflow-main" :class="{ 'no-selector': !showSlideSelector }">
              <SlideStrip
                v-if="showSlideSelector"
                :slides="slides"
                :selected-index="selectedSlideIndex"
                :width="sidebarWidth"
                :show-render-state="activeTab === 'preview'"
                :rendered-page-videos="renderedPageVideos"
                :rendering-page-status="renderingPageStatus"
                :variant-counts="variantCountsByPage"
                :has-merged-preview="activeTab === 'preview' && hasMergedPreview"
                :merged-preview-thumbnail-url="mergedPreviewThumbnailDisplayUrl"
                :selected-merged-preview="isMergedPreviewSelected"
                @update:selected-index="handleSlideIndexChange"
                @select-merged-preview="selectMergedPreviewPage"
                @resize-start="startXDrag"
              />

              <section class="content-panel">
                <!-- Script Editor -->
                <div v-if="activeTab === 'script'" class="script-view" :style="{ '--preview-height': previewHeight + 'px' }">
                  <div class="preview-panel">
                    <img v-if="selectedSlidePreviewUrl" :src="selectedSlidePreviewUrl" alt="slide" />
                    <div v-else class="placeholder-text">{{ selectedSlideImageError || '尚無頁面可預覽' }}</div>
                    <div v-if="selectedSlideImageError" class="alert alert-danger mt-2 mb-0 py-2">{{ selectedSlideImageError }}</div>
                    <div v-if="selectedSlide" class="script-generation-actions">
                      <div class="script-generation-buttons">
                        <button
                          class="script-gen-btn"
                          :disabled="scriptGenerating || !llmConfigured"
                          :title="llmConfigured ? `AI 生成第 ${selectedSlideIndex + 1} 頁講稿（只覆蓋本頁）` : 'LLM 尚未設定'"
                          @click="generateScriptForCurrentPage"
                        >{{ scriptGenerating ? '生成中…' : 'AI 生成本頁講稿' }}</button>
                        <button
                          class="script-gen-btn secondary"
                          :disabled="scriptGenerating || !llmConfigured"
                          :title="llmConfigured ? 'AI 重新生成全部講稿（會覆蓋既有內容）' : 'LLM 尚未設定'"
                          @click="generateScriptsForAllPages"
                        >{{ scriptGenerating ? '生成中…' : 'AI 生成全部講稿' }}</button>
                      </div>
                    </div>
                  </div>

                  <div
                    class="resizer-y"
                    title="拖曳調整預覽與講稿區比例"
                    @mousedown.prevent="startYDrag"
                    @touchstart.prevent="startYDrag"
                  ></div>

                  <textarea
                    v-if="selectedSlide"
                    v-model="selectedSlide.scriptText"
                    class="form-control script-area"
                    placeholder="請輸入或修改此頁逐字稿..."
                  />
                </div>

                <!-- Subtitle Settings -->
                <div v-else-if="activeTab === 'subtitle'" class="subtitle-editor-layout simple-subtitle-layout">
                  <SubtitleSettingsPanel
                    :settings="globalSettings.subtitle"
                    v-model:enable-highlight="enableSubtitleHighlight"
                    v-model:output-mode="globalSettings.subtitle.outputMode"
                  />

                  <div class="subtitle-preview-right">
                    <div class="subtitle-canvas-frame">
                      <div ref="subtitlePreviewContainerRef" class="subtitle-canvas-wrapper">
                        <img v-if="selectedSlidePreviewUrl" :src="selectedSlidePreviewUrl" class="subtitle-canvas-img" alt="slide-bg" />
                        <div v-else class="placeholder-text text-secondary subtitle-canvas-placeholder">（請先選擇投影片畫面）</div>
                        <div class="subtitle-fast-overlay" :style="subtitleFastOverlayStyle">
                          <span class="subtitle-fast-box" :style="subtitleFastBoxStyle">
                            <span
                              v-for="(ch, i) in subtitleFastChars"
                              :key="`fast-${i}`"
                              :style="(enableSubtitleHighlight && i === subtitleHighlightLoopIndex) ? subtitleFastActiveCharStyle : null"
                            >{{ ch }}</span>
                          </span>
                        </div>
                        <canvas ref="subtitleAssCanvasRef" width="1280" height="720" class="subtitle-ass-canvas d-none"></canvas>
                      </div>
                    </div>
                  </div>
                </div>

                <VoiceSettingsPanel
                  v-else-if="activeTab === 'tts'"
                  :preset-manifest="presetManifest"
                  v-model:selected-voice-key="selectedVoiceKey"
                  :preset-loading="presetLoading"
                  :preset-load-error="presetLoadError"
                  :is-preset-voice="isPresetVoice"
                  :clone-audio-url="cloneAudioUrl"
                  :clone-audio-file="cloneAudioFile"
                  v-model:reference-text="referenceText"
                  :asr-filling="asrFilling"
                  v-model:tts-preview-text="ttsPreviewText"
                  v-model:tts-preview-speed="ttsPreviewSpeed"
                  :tts-generating="ttsGenerating"
                  :tts-preview-url="ttsPreviewUrl"
                  :tts-error="ttsError"
                  @voice-change="onVoiceKeyChange"
                  @ref-audio-drop="onRefAudioDrop"
                  @clone-file-change="onCloneFileChange"
                  @clear-clone-audio="clearCloneAudio"
                  @fill-reference-text="fillReferenceTextWithLocalAsr"
                  @generate-preview="generateTtsPreview"
                />

                <!-- Single Page Preview -->
                <div v-else class="panel-view d-flex flex-column gap-3">
                  <div class="preview-output-layout">
                    <div
                      v-if="hasVariantDrawer"
                      class="variant-drawer"
                      :class="{ collapsed: !variantDrawerOpen }"
                    >
                      <div class="variant-drawer-clip">
                        <div class="variant-drawer-body">
                          <PageVariantPanel
                            v-if="!isMergedPreviewSelected"
                            :run-id="currentRunId || ''"
                            :page-index="selectedSlideIndex"
                            :variants="selectedPageVariants"
                            :selected-variant-id="selectedPageVariantId"
                            :chunk-regenerating="rendering"
                            @select="selectPageVariant"
                            @delete="deletePageVariant"
                            @regenerate-chunk="regenerateTtsChunk"
                          />
                          <PageVariantPanel
                            v-else
                            :run-id="currentRunId || ''"
                            title="ALL"
                            kicker="合併影片"
                            :variants="exportVariants"
                            :selected-variant-id="selectedExportVariantId"
                            @select="selectMergedExportVariant"
                            @delete="deleteMergedExportVariant"
                          />
                        </div>
                      </div>
                      <button
                        type="button"
                        class="variant-drawer-toggle"
                        :title="variantDrawerOpen ? '收起產出影片' : '展開產出影片'"
                        :aria-label="variantDrawerOpen ? '收起產出影片' : '展開產出影片'"
                        @click="variantDrawerOpen = !variantDrawerOpen"
                      ><span class="variant-drawer-chevron" :class="{ open: variantDrawerOpen }" aria-hidden="true"></span></button>
                    </div>
                    <div class="preview-render-zone">
                      <div class="preview-panel final flex-grow-1">
                        <video
                          v-if="selectedRenderedVideoUrl"
                          :src="selectedRenderedVideoUrl"
                          class="preview-output-video"
                          controls
                          preload="metadata"
                        />
                        <img v-if="!selectedRenderedVideoUrl && selectedSlidePreviewUrl" :src="selectedSlidePreviewUrl" alt="final" />
                        <div v-else-if="!selectedRenderedVideoUrl" class="placeholder-text">{{ selectedSlideImageError || '尚無頁面可預覽' }}</div>
                        <div v-if="selectedSlideImageError" class="alert alert-danger mt-2 mb-0 py-2">{{ selectedSlideImageError }}</div>
                      </div>

                      <RenderControls
                        v-if="!isMergedPreviewSelected"
                        :has-selected-slide="!!selectedSlide"
                        :has-rendered-video="!!selectedRenderedVideoUrl"
                        :rendered-count="Object.keys(renderedPageVideos).length"
                        :slides-count="slides.length"
                        :rendering="rendering"
                        :rendering-all="renderingAll"
                        :queue-length="singleRenderQueue.length"
                        :cancellable-single-pages="cancellableSinglePages"
                        :message="renderMessage"
                        :download-video-url="selectedPageDownloadVideoUrl"
                        :download-srt-url="selectedPageDownloadSrtUrl"
                        :download-bundle-url="selectedPageDownloadBundleUrl"
                        @render-current="renderCurrentPage"
                        @render-all="renderAllPages"
                        @stop-all="requestStopAllRendering"
                        @stop-page="requestStopPage"
                        @merge="mergeAndDownloadRenderedVideos"
                      />
                      <div v-else class="merged-export-controls">
                        <DownloadMenu
                          label="下載合併檔案"
                          :video-url="exportVideoUrl(selectedExportVariantId)"
                          :srt-url="hasSelectedExportSrt ? exportSrtUrl(selectedExportVariantId) : ''"
                          :bundle-url="exportBundleUrl(selectedExportVariantId)"
                        />
                      </div>
                    </div>
                  </div>
                </div>
              </section>
            </div>
          </div>
        </div>
      </div>
      <div v-else-if="openingProject" class="workspace-stage project-loading-stage">
        <div class="placeholder-text">載入專案圖片中…</div>
      </div>
    </div>
  </AppShell>
</template>

<script setup>
import { createImageQueue } from '../utils/image-queue.js'
import { computed, onBeforeUnmount, onMounted, ref, watch, nextTick } from 'vue'
import AppShell from './AppShell.vue'
import PageVariantPanel from './PageVariantPanel.vue'
import DownloadMenu from './DownloadMenu.vue'
import RenderControls from './RenderControls.vue'
import SlideStrip from './SlideStrip.vue'
import SubtitleSettingsPanel from './SubtitleSettingsPanel.vue'
import VoiceSettingsPanel from './VoiceSettingsPanel.vue'
import { useProjectSettings } from '../composables/useProjectSettings.js'
import { useRenderQueue } from '../composables/useRenderQueue.js'
import { useVideoRuns } from '../composables/useVideoRuns.js'
import { API_ENDPOINTS, clearEndpointCache, getApiEndpoint } from '../config/api.js'
import { emitter } from '../config/events.js'
import {
  DEFAULTS as SHARED_DEFAULTS,
  isPuncOrSpace as sharedIsPuncOrSpace,
  tokenizeSubtitlePieces as sharedTokenizeSubtitlePieces,
} from '../../../shared/subtitle-layout/index.js'

const stage = ref('upload')
const openingProject = ref(false)
const activeTab = ref('script')

const fileInput = ref(null)
const pdfFile = ref(null)
const pdfId = ref(null)
const currentRunId = ref(null)

const uploading = ref(false)
const uploadPhase = ref('')
const rendering = ref(false)
const renderingAll = ref(false)
const scriptGenerating = ref(false)
const llmStatus = ref({ configured: null, provider: '', model: '' })
const statusMessage = ref('')
const errorMessage = ref('')
const renderMessage = ref('')

const contentLanguage = ref('zh')
const subtitleSource = ref('zh')
const userProvidedScript = ref('')
const selectedSlideIndex = ref(0)
const slides = ref([])
const tempObjectUrls = ref([])
const renderedPageVideos = ref({})
const mergedPreviewVideoUrl = ref('')
const mergedPreviewThumbnailUrl = ref('')
const isMergedPreviewSelected = ref(false)
const variantDrawerOpen = ref(false)
const runManifest = ref(null)
const selectedVariantIds = ref({})
const suppressScriptSave = ref(false)
let scriptSaveTimer = null

// Resizer logic
const sidebarWidth = ref(220)
const previewHeight = ref(350)
let startX = 0, startWidth = 0
let startY = 0, startHeight = 0

// 字幕相關狀態
const subtitleAudioInput = ref(null)
const subtitleAudioFile = ref(null)
const subtitleAudioUrl = ref('')
const subtitleAlignText = ref('')
const DEFAULT_SUBTITLE_SPLIT_MIN_CHARS = 10
const DEFAULT_SUBTITLE_SPLIT_MAX_CHARS = 32
const subtitleAligning = ref(false)
const subtitleAligningStage = ref('')  // 顯示當前對齊階段
const subtitleAlignError = ref('')
const subtitleAlignWarning = ref('')
const subtitleAlignedSegments = ref([])       // 後端回傳的有時間戳資料
const subtitleAlignBackend = ref('')          // 紀錄後端使用的切分器（供除錯用）
const subtitleAlignSrt = ref('')              // 後端回傳的 SRT 字幕內容

// 新增的字幕樣式設定狀態
const subtitleStyle = ref('bg-dark')
const enableSubtitleHighlight = ref(false)

const onXDrag = (e) => {
  const delta = e.clientX - startX
  sidebarWidth.value = Math.max(100, Math.min(startWidth + delta, 450)) // 最小值 100px
}
const stopXDrag = () => {
  document.removeEventListener('mousemove', onXDrag)
  document.removeEventListener('mouseup', stopXDrag)
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
}
const startXDrag = (e) => {
  startX = e.clientX
  startWidth = sidebarWidth.value
  document.addEventListener('mousemove', onXDrag)
  document.addEventListener('mouseup', stopXDrag)
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}

const eventClientY = (e) => {
  if (e?.touches?.length) return e.touches[0].clientY
  if (e?.changedTouches?.length) return e.changedTouches[0].clientY
  return e?.clientY || 0
}

const onYDrag = (e) => {
  const delta = eventClientY(e) - startY
  previewHeight.value = Math.max(150, Math.min(startHeight + delta, window.innerHeight * 0.75))
}
const stopYDrag = () => {
  document.removeEventListener('mousemove', onYDrag)
  document.removeEventListener('mouseup', stopYDrag)
  document.removeEventListener('touchmove', onYDrag)
  document.removeEventListener('touchend', stopYDrag)
  document.removeEventListener('touchcancel', stopYDrag)
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
}
const startYDrag = (e) => {
  startY = eventClientY(e)
  startHeight = previewHeight.value
  document.addEventListener('mousemove', onYDrag)
  document.addEventListener('mouseup', stopYDrag)
  document.addEventListener('touchmove', onYDrag, { passive: false })
  document.addEventListener('touchend', stopYDrag)
  document.addEventListener('touchcancel', stopYDrag)
  document.body.style.cursor = 'row-resize'
  document.body.style.userSelect = 'none'
}

const subtitlePreviewContainerRef = ref(null) // 預覽圖容器（用於計算字幕比例大小）
const subtitleAssCanvasRef = ref(null)
const subtitlePreviewContainerWidth = ref(0)
const subtitlePreviewContainerHeight = ref(0)
const subtitlePreviewResizeObserver = ref(null)
let subtitleAssEngine = null
let subtitleAssScriptPromise = null
let subtitleAssRerenderTimer = null
let subtitleAssCanvasPixelW = 0
let subtitleAssCanvasPixelH = 0
let subtitleAssLastContent = ''
const USE_FAST_SUBTITLE_PREVIEW = true
const cloneAudioUrl = ref('')
const cloneAudioFile = ref(null)
const subtitleDemoAudioRef = ref(null)
const subtitleDemoCurrentTime = ref(0)
const subtitleDemoDuration = ref(0)
const subtitleDemoPlaying = ref(false)
const subtitleDemoRafId = ref(null)

// 字幕設定小工具
const subtitleTestText = ref('')

// TTS 試聽面板狀態
const ttsPreviewText = ref('')
const ttsPreviewUrl = ref('')
const ttsGenerating = ref(false)
const ttsError = ref('')
const referenceText = ref('')
const asrFilling = ref(false)

// 音色來源狀態
const selectedVoiceKey = ref('')  // 預設為空，等待 manifest 載入後設為第一個
const presetLoading   = ref(false)
const presetLoadError = ref(false)
const TTS_PREVIEW_SPEED_MIN = 0.5
const TTS_PREVIEW_SPEED_MAX = 2.0

// 是否為預設音色（唯讀模式）
const isPresetVoice = computed(() => selectedVoiceKey.value !== 'custom')

// 預設音色的 transcript 快取
const presetManifest = ref({})
let schedulePersistRunSettings = () => {}
let persistRunSettingsImmediately = async () => true
let qwenTtsWarmupTimer = null

const scheduleQwenTtsWarmup = (delay = 250) => {
  if (qwenTtsWarmupTimer) clearTimeout(qwenTtsWarmupTimer)
  qwenTtsWarmupTimer = setTimeout(async () => {
    qwenTtsWarmupTimer = null
    try {
      await fetch(getApiEndpoint('/api/video-abstract/tts-warmup'), {
        method: 'POST',
      })
    } catch (e) {
      console.warn('[Qwen3 TTS] warmup failed:', e)
    }
  }, delay)
}

const _loadManifest = async () => {
  if (Object.keys(presetManifest.value).length > 0) return
  try {
    // 加上時間戳避免瀏覽器快取舊的 json
    const resp = await fetch(getApiEndpoint(`/static/ref_voices/manifest.json?t=${Date.now()}`))
    if (resp.ok) {
      presetManifest.value = await resp.json()
    }
  } catch (e) {
    console.warn('[PresetVoice] manifest load failed:', e)
  }
}

// 載入預設音色檔案並設定為 cloneAudioFile + cloneAudioUrl，同時自動填入參考文字
const loadPresetVoice = async (key) => {
  if (!presetManifest.value[key]) return
  const fileName = presetManifest.value[key].file
  const url = getApiEndpoint(`/static/ref_voices/${fileName}`)
  presetLoading.value   = true
  presetLoadError.value = false
  if (cloneAudioUrl.value) { URL.revokeObjectURL(cloneAudioUrl.value); cloneAudioUrl.value = '' }
  cloneAudioFile.value = null
  referenceText.value = ''
  try {
    // 同時請求 manifest（首次才實際發請求，之後從快取讀）
    await _loadManifest()
    const resp = await fetch(url)
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const file = new File([blob], `${key}.wav`, { type: 'audio/wav' })
    cloneAudioFile.value = file
    const objUrl = URL.createObjectURL(blob)
    cloneAudioUrl.value = objUrl
    tempObjectUrls.value.push(objUrl)
    // 自動填入對應的參考文字（唯讀，不允許使用者修改）
    const entry = presetManifest.value[key]
    if (entry?.transcript) referenceText.value = entry.transcript
    scheduleQwenTtsWarmup()
  } catch (e) {
    console.warn('[PresetVoice] load failed:', e)
    presetLoadError.value = true
  } finally {
    presetLoading.value = false
  }
}

// 音色選單變化處理
const onVoiceKeyChange = async (nextKey = selectedVoiceKey.value) => {
  selectedVoiceKey.value = nextKey
  if (selectedVoiceKey.value === 'custom') {
    // 自訂模式：清空預設載入的音檔/文字/錯誤，等待使用者上傳
    if (cloneAudioUrl.value) { URL.revokeObjectURL(cloneAudioUrl.value); cloneAudioUrl.value = '' }
    cloneAudioFile.value = null
    referenceText.value = ''
    presetLoadError.value = false
    schedulePersistRunSettings()
  } else {
    await loadPresetVoice(selectedVoiceKey.value)
    schedulePersistRunSettings()
  }
}

// 自訂音色刪除
const clearCloneAudio = () => {
  if (cloneAudioUrl.value) { URL.revokeObjectURL(cloneAudioUrl.value); cloneAudioUrl.value = '' }
  cloneAudioFile.value = null
  referenceText.value = ''
  schedulePersistRunSettings()
}

const createDefaultGlobalSettings = () => ({
  subtitle: {
    fontSize: 52,
    color: '#ffffff',
    activeWordColor: '#facc15',
    enableBackground: true,
    bgColor: '#000000',
    bgOpacity: 55,
    marginV: 90,
    enableOutline: false,
    outlineColor: '#000000',
    outlineWidth: 2,
    outputMode: 'burn',
  },
  tts: {
    model: 'voxcpm',
    voice: 'zh-TW-YunJheNeural',
    speed: 1.0,
  },
})

const globalSettings = ref(createDefaultGlobalSettings())

const clampTtsPreviewSpeed = (value) => {
  const num = Number(value)
  if (!Number.isFinite(num)) return 1.0
  return Math.max(TTS_PREVIEW_SPEED_MIN, Math.min(TTS_PREVIEW_SPEED_MAX, num))
}

const ttsPreviewSpeed = computed({
  get: () => clampTtsPreviewSpeed(globalSettings.value.tts.speed),
  set: (value) => {
    globalSettings.value.tts.speed = clampTtsPreviewSpeed(value)
  },
})
const uploadButtonText = computed(() => uploadPhase.value || '處理中...')
const llmConfigured = computed(() => llmStatus.value.configured === true)
const showSlideSelector = computed(() => activeTab.value === 'script' || activeTab.value === 'preview')
const selectedSlide = computed(() => (isMergedPreviewSelected.value ? null : (slides.value[selectedSlideIndex.value] || null)))

const {
  runPages,
  selectedPageVariants,
  selectedPageVariantId,
  variantCountsByPage,
  exportVariants,
  selectedExportVariantId,
  variantVideoUrl,
  variantSrtUrl,
  variantBundleUrl,
  exportVideoUrl,
  exportSrtUrl,
  exportBundleUrl,
  refreshRunManifest,
  selectPageVariant,
  deletePageVariant,
  selectExportVariant,
  deleteExportVariant,
} = useVideoRuns({
  currentRunId,
  runManifest,
  selectedVariantIds,
  renderedPageVideos,
  selectedSlideIndex,
  getApiEndpoint,
  renderMessage,
})

const selectedPageVariant = computed(() => selectedPageVariants.value.find(
  (variant) => variant.variant_id === selectedPageVariantId.value,
) || null)
const hasSelectedSrt = computed(() => Boolean(selectedPageVariant.value?.paths?.srt))
const selectedPageDownloadVideoUrl = computed(() => variantVideoUrl(selectedSlideIndex.value, selectedPageVariantId.value) || selectedRenderedVideoUrl.value || '')
const selectedPageDownloadSrtUrl = computed(() => hasSelectedSrt.value ? variantSrtUrl(selectedSlideIndex.value, selectedPageVariantId.value) : '')
const selectedPageDownloadBundleUrl = computed(() => variantBundleUrl(selectedSlideIndex.value, selectedPageVariantId.value))
const selectedExportVariant = computed(() => exportVariants.value.find(
  (variant) => variant.variant_id === selectedExportVariantId.value,
) || null)
const hasSelectedExportSrt = computed(() => Boolean(selectedExportVariant.value?.paths?.srt))

const hasMergedPreview = computed(() => Boolean(
  mergedPreviewVideoUrl.value || selectedExportVariantId.value || exportVariants.value.length,
))
const mergedPreviewThumbnailDisplayUrl = computed(() => (
  mergedPreviewThumbnailUrl.value
  || slides.value[0]?.thumbnailUrl
  || slides.value[0]?.previewUrl
  || ''
))
const sanitizeDownloadName = (value, fallback = 'video') => {
  const cleaned = String(value || '')
    .replace(/\.[Pp][Dd][Ff]$/, '')
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
  return cleaned || fallback
}
const getMergedDownloadFilename = () => {
  const baseName = sanitizeDownloadName(
    runManifest.value?.display_name
      || runManifest.value?.original_filename
      || pdfFile.value?.name
      || currentRunId.value,
    'merged_video',
  )
  return `${baseName}.mp4`
}
const selectedSlidePreviewUrl = computed(() => {
  if (isMergedPreviewSelected.value) return mergedPreviewThumbnailUrl.value || slides.value[0]?.thumbnailUrl || ''
  return selectedSlide.value?.previewUrl || selectedSlide.value?.thumbnailUrl || ''
})
const selectedSlideImageError = computed(() => selectedSlide.value?.previewError || selectedSlide.value?.thumbnailError || '')
const selectedRenderedVideoUrl = computed(() => {
  if (isMergedPreviewSelected.value) {
    return mergedPreviewVideoUrl.value || exportVideoUrl(selectedExportVariantId.value) || ''
  }
  return renderedPageVideos.value[selectedSlideIndex.value] || ''
})

const hasVariantDrawer = computed(() => isMergedPreviewSelected.value
  ? exportVariants.value.length > 0
  : selectedPageVariants.value.length > 0)

const {
  singleRenderQueue,
  renderingPageStatus,
  cancellableSinglePages,
  requestStopAllRendering,
  requestStopPage,
  renderCurrentPage,
  renderAllPages,
  reattachActiveBatchJob,
  regenerateTtsChunk,
  mergeAndDownloadRenderedVideos,
} = useRenderQueue({
  slides,
  selectedSlide,
  selectedSlideIndex,
  renderedPageVideos,
  rendering,
  renderingAll,
  renderMessage,
  currentRunId,
  globalSettings,
  referenceText,
  cloneAudioFile,
  selectedVoiceKey,
  enableSubtitleHighlight,
  subtitleOutputMode: computed(() => globalSettings.value.subtitle.outputMode || 'burn'),
  selectedVariantIds,
  refreshRunManifest,
  getMergedDownloadFilename,
  getApiEndpoint,
  emitter,
  splitMinChars: DEFAULT_SUBTITLE_SPLIT_MIN_CHARS,
  splitMaxChars: DEFAULT_SUBTITLE_SPLIT_MAX_CHARS,
  persistRunSettingsNow: (options) => persistRunSettingsImmediately(options),
  onMergedPreviewReady: (url, exportVariantId = '') => {
    mergedPreviewVideoUrl.value = String(url || '')
    mergedPreviewThumbnailUrl.value = selectedSlide.value?.previewUrl || selectedSlide.value?.thumbnailUrl || slides.value[0]?.thumbnailUrl || ''
    isMergedPreviewSelected.value = true
    activeTab.value = 'preview'
  },
})

const projectSettings = useProjectSettings({
  currentRunId,
  stage,
  runManifest,
  globalSettings,
  enableSubtitleHighlight,
  selectedVoiceKey,
  referenceText,
  cloneAudioFile,
  cloneAudioUrl,
  tempObjectUrls,
  renderMessage,
  getApiEndpoint,
  onVoiceKeyChange,
})

const {
  suppressProjectSettingsSave,
  applyVariantSettingsToUi,
  applyProjectSettingsToUi,
  clearProjectSettingsSaveTimer,
} = projectSettings
schedulePersistRunSettings = projectSettings.schedulePersistRunSettings
persistRunSettingsImmediately = projectSettings.persistRunSettingsNow

const HIGHLIGHT_LEAD_SEC = SHARED_DEFAULTS.HIGHLIGHT_LEAD_SEC
const LAST_TOKEN_HOLD_SEC = SHARED_DEFAULTS.LAST_TOKEN_HOLD_SEC
const SEGMENT_LINGER_SEC = SHARED_DEFAULTS.SEGMENT_LINGER_SEC
const SUBTITLE_ACTIVE_TOLERANCE_SEC = SHARED_DEFAULTS.SUBTITLE_ACTIVE_TOLERANCE_SEC
const DEFAULT_SUBTITLE_MIN_WORD_MS = SHARED_DEFAULTS.DEFAULT_SUBTITLE_MIN_WORD_MS
const isPuncOrSpace = sharedIsPuncOrSpace
const tokenizeSubtitlePieces = sharedTokenizeSubtitlePieces

const normalizedSubtitleSegments = computed(() => {
  return (Array.isArray(subtitleAlignedSegments.value) ? subtitleAlignedSegments.value : [])
    .map((seg) => {
      const words = Array.isArray(seg?.words)
        ? seg.words
            .map((w) => ({
              text: String(w?.text || '').trim(),
              start: Number(w?.start),
              end: Number(w?.end),
            }))
            .filter((w) => w.text && Number.isFinite(w.start) && Number.isFinite(w.end) && w.end > w.start)
            .sort((a, b) => a.start - b.start)
        : []

      const segStart = Number(seg?.start) || 0
      const segEnd = Number(seg?.end) || 0
      const wordStart = words.length ? Number(words[0].start) : segStart
      const wordEnd = words.length ? Number(words[words.length - 1].end) : segEnd
      return {
        start: wordStart,
        end: wordEnd,
        text: String(seg?.text || '').trim(),
        words,
      }
    })
    .filter((seg) => seg.end > seg.start && seg.text.length > 0)
    .sort((a, b) => a.start - b.start)
})

const syncedSubtitleText = computed(() => {
  return activeSubtitleSegment.value?.text || ''
})

const subtitleAlignedTimeSec = computed(() => {
  return Number(subtitleDemoCurrentTime.value)
})
const isQwenAlignBackend = computed(() =>
  String(subtitleAlignBackend.value || '').toLowerCase().includes('qwen3'),
)

const activeSubtitleSegment = computed(() => {
  const segs = normalizedSubtitleSegments.value
  if (!segs.length) return null
  const current = subtitleAlignedTimeSec.value
  // For Qwen3 forced-alignment, timestamps are precise: use zero tolerance
  // so segments never appear before the audio actually reaches them.
  const tol = isQwenAlignBackend.value ? 0.0 : SUBTITLE_ACTIVE_TOLERANCE_SEC
  const strictIdx = segs.findIndex((seg) => current >= seg.start - tol && current < seg.end + tol)
  if (strictIdx >= 0) return segs[strictIdx]
  return null
})

const hasCjkChar = (text) => /[\u3400-\u9fff]/.test(String(text || ''))

const resolveActiveWordIndex = (words, currentSec, strictMode = false) => {
  const now = Number(currentSec) || 0
  if (!Array.isArray(words) || words.length === 0) return -1

  // 1) Strict hit: word is currently being spoken
  let idx = words.findIndex((w) => now >= Number(w.start) && now < Number(w.end))
  if (idx >= 0) return idx

  // 2) Gap-fill: extend the END by EPS to handle tiny boundary gaps between words.
  //    For Qwen3 (strictMode), use a small but non-zero EPS so the last character
  //    and short words like "的" don't lose highlight at the exact end boundary.
  const EPS = strictMode ? 0.06 : 0.08  // 60-80ms gap fill
  idx = words.findIndex((w) => now >= Number(w.start) && now < Number(w.end) + EPS)
  if (idx >= 0) return idx

  // 3) Backward-biased fallback: keep the most recent past word.
  let prevIdx = -1
  for (let i = 0; i < words.length; i += 1) {
    const s = Number(words[i].start)
    if (!Number.isFinite(s)) continue
    if (s <= now) prevIdx = i
    else break
  }
  if (prevIdx >= 0) return prevIdx
  return -1
}

const buildSmoothedWordTimeline = (words, segStart, segEnd, minWordMs = 90) => {
  if (!Array.isArray(words) || words.length === 0) return []
  const n = words.length
  const start = Number(segStart) || 0
  const end = Number(segEnd) || start
  const segDur = Math.max(0.001, end - start)
  const requestedMin = Math.max(0.03, (Number(minWordMs) || 90) / 1000)
  const feasibleMin = Math.max(0.02, segDur / Math.max(n, 1) * 0.9)
  const minDur = Math.min(requestedMin, feasibleMin)
  const uniformDur = segDur / n
  // Favor stable visual pacing over noisy raw boundaries.
  const blendRaw = 0.25

  const boundaries = new Array(n + 1).fill(0)
  boundaries[0] = start
  boundaries[n] = end

  for (let i = 1; i < n; i += 1) {
    const prev = words[i - 1]
    const cur = words[i]
    const prevEnd = Number(prev?.end)
    const curStart = Number(cur?.start)
    const rawMid = Number.isFinite(prevEnd) && Number.isFinite(curStart)
      ? (prevEnd + curStart) / 2
      : start + i * uniformDur
    const uniMid = start + i * uniformDur
    boundaries[i] = (blendRaw * rawMid) + ((1 - blendRaw) * uniMid)
  }

  for (let i = 1; i <= n; i += 1) {
    boundaries[i] = Math.max(boundaries[i], boundaries[i - 1] + minDur)
  }
  boundaries[n] = end
  for (let i = n - 1; i >= 0; i -= 1) {
    boundaries[i] = Math.min(boundaries[i], boundaries[i + 1] - minDur)
  }
  boundaries[0] = start
  boundaries[n] = end

  const out = []
  for (let i = 0; i < n; i += 1) {
    const s = Math.max(start, boundaries[i])
    let e = Math.min(end, boundaries[i + 1])
    if (e <= s) {
      e = Math.min(end, s + Math.max(minDur * 0.5, 0.02))
    }
    out.push({
      text: String(words[i]?.text || '').trim(),
      start: s,
      end: e,
    })
  }
  if (out.length > 0) {
    const lastIdx = out.length - 1
    const endCap = end + LAST_TOKEN_HOLD_SEC
    out[lastIdx].end = Math.max(out[lastIdx].end, Math.min(endCap, out[lastIdx].end + LAST_TOKEN_HOLD_SEC))
  }
  return out
}

const buildRawWordTimeline = (words, segEnd, minWordMs = 90) => {
  if (!Array.isArray(words) || words.length === 0) return []
  const minDur = Math.max(0.02, (Number(minWordMs) || 90) / 1000 * 0.5)
  const out = []
  let lastEnd = -Infinity
  for (let i = 0; i < words.length; i += 1) {
    const text = String(words[i]?.text || '').trim()
    if (!text) continue
    let s = Number(words[i]?.start)
    let e = Number(words[i]?.end)
    if (!Number.isFinite(s)) s = Number.isFinite(lastEnd) ? lastEnd : 0
    if (!Number.isFinite(e) || e <= s) e = s + minDur
    if (Number.isFinite(lastEnd) && s < lastEnd) s = lastEnd
    if (e <= s) e = s + minDur
    out.push({ text, start: s, end: e })
    lastEnd = e
  }
  if (out.length > 0) {
    const lastIdx = out.length - 1
    const endCapBase = Number(segEnd)
    const endCap = Number.isFinite(endCapBase)
      ? endCapBase + LAST_TOKEN_HOLD_SEC
      : out[lastIdx].end + LAST_TOKEN_HOLD_SEC
    out[lastIdx].end = Math.max(out[lastIdx].end, Math.min(endCap, out[lastIdx].end + LAST_TOKEN_HOLD_SEC))
  }
  return out
}

const subtitleOverlayPieces = computed(() => {
  const seg = activeSubtitleSegment.value
  const inSubtitleLivePreview = activeTab.value === 'subtitle' && (subtitleDemoPlaying.value || subtitleDemoCurrentTime.value > 0)
  if (!inSubtitleLivePreview || !seg?.text) return []
  const current = subtitleAlignedTimeSec.value
  const effectiveNow = Math.min(Number(seg.end) - 0.001, current + HIGHLIGHT_LEAD_SEC)

  // For CJK, prefer stable per-character highlighting within segment time range.
  // ASR word boundaries for CJK can be noisy and may skip characters visually.
  const preferCharStable = hasCjkChar(seg.text)
  const canUseWordTimeline = Array.isArray(seg.words) && seg.words.length > 0
  // Qwen3 對齊下，已經保證 100% 絕對時間準確，因此不要再過任何 Smoothing 或 Margin
  if (canUseWordTimeline && (!preferCharStable || isQwenAlignBackend.value)) {
    const timelineWords = isQwenAlignBackend.value
      ? seg.words
      : buildSmoothedWordTimeline(seg.words, seg.start, seg.end, DEFAULT_SUBTITLE_MIN_WORD_MS)
    // Pass strictMode=true for Qwen3 to prevent any forward peeking
    let activeWordIdx = resolveActiveWordIndex(timelineWords, effectiveNow, isQwenAlignBackend.value)
    if (activeWordIdx < 0 && current >= Number(seg.end) - SEGMENT_LINGER_SEC) {
      activeWordIdx = timelineWords.length - 1
    }
    const pieces = []
    for (let i = 0; i < timelineWords.length; i += 1) {
      const w = timelineWords[i]
      const subPieces = tokenizeSubtitlePieces(w.text)
      for (const sp of subPieces) {
        pieces.push({
          text: sp.text,
          highlightable: sp.highlightable,
          active: sp.highlightable && (i === activeWordIdx)
        })
      }
      if (i < timelineWords.length - 1) {
        const nextText = timelineWords[i + 1].text
        const t1 = w.text.slice(-1)
        const t2 = nextText.charAt(0)
        const isAscii1 = /[A-Za-z0-9]/.test(t1)
        const isAscii2 = /[A-Za-z0-9]/.test(t2)
        const isCjk1 = /[\u3400-\u9fff]/.test(t1)
        const isCjk2 = /[\u3400-\u9fff]/.test(t2)
        if ((isAscii1 && isAscii2) || (isCjk1 && isAscii2) || (isAscii1 && isCjk2)) {
          pieces.push({ text: ' ', highlightable: false, active: false })
        }
      }
    }
    return pieces
  }

  const pieces = tokenizeSubtitlePieces(seg.text)
  const hlCount = pieces.filter((p) => p.highlightable).length
  if (hlCount <= 0) return pieces.map((p) => ({ ...p, active: false }))

  const segDur = Math.max(0.001, Number(seg.end) - Number(seg.start))
  const progress = Math.max(0, Math.min(1, (effectiveNow - Number(seg.start)) / segDur))
  const activeIdx = Math.min(hlCount - 1, Math.floor(progress * hlCount))

  let seen = -1
  return pieces.map((p) => {
    if (!p.highlightable) return { ...p, active: false }
    seen += 1
    return { ...p, active: seen === activeIdx }
  })
})

const currentSpokenToken = computed(() => {
  const active = subtitleOverlayPieces.value.find((p) => p.active)
  return active?.text?.trim() || ''
})

const subtitleActivePieceStyle = computed(() => ({
  color: globalSettings.value.subtitle.activeWordColor || '#facc15',
  fontWeight: 700,
  textShadow: '0 1px 10px rgba(0,0,0,0.65)',
}))

const subtitleOverlayDisplayText = computed(() => {
  const inSubtitleLivePreview = activeTab.value === 'subtitle' && (subtitleDemoPlaying.value || subtitleDemoCurrentTime.value > 0)
  if (inSubtitleLivePreview) {
    // Keep empty when there is no active segment, instead of falling back to preview text.
    // This avoids "suddenly jumping back to default subtitle" during long pauses.
    return syncedSubtitleText.value || ''
  }
  // Only show user-typed preview text — no default placeholder. Overlay is blank by default.
  return subtitleTestText.value || ''
})

const subtitleOverlayVisible = computed(() => {
  const inSubtitleLivePreview = activeTab.value === 'subtitle' && (subtitleDemoPlaying.value || subtitleDemoCurrentTime.value > 0)
  if (!inSubtitleLivePreview) {
    // Outside of live preview: only show if user typed something
    return Boolean((subtitleOverlayDisplayText.value || '').trim())
  }
  if (subtitleOverlayPieces.value.length > 0) return true
  return Boolean((subtitleOverlayDisplayText.value || '').trim())
})

// 自動載入預設音色
onMounted(async () => {
    try {
      const response = await fetch(getApiEndpoint('/api/llm/status'))
      llmStatus.value = response.ok
        ? await response.json()
        : { configured: null, provider: '', model: '' }
    } catch {
      llmStatus.value = { configured: null, provider: '', model: '' }
    }
    // 載入 TTS 預設音色 manifest
    await _loadManifest()
    if (Object.keys(presetManifest.value).length > 0) {
      selectedVoiceKey.value = Object.keys(presetManifest.value)[0]
      await loadPresetVoice(selectedVoiceKey.value)
    }
})

// Monitor preview container size to scale subtitle font proportionally
const startPreviewContainerObserver = (el) => {
  if (!el) return
  if (subtitlePreviewResizeObserver.value) {
    subtitlePreviewResizeObserver.value.disconnect()
  }
  const ro = new ResizeObserver((entries) => {
    for (const entry of entries) {
      subtitlePreviewContainerWidth.value = entry.contentRect.width
      subtitlePreviewContainerHeight.value = entry.contentRect.height
    }
  })
  ro.observe(el)
  subtitlePreviewResizeObserver.value = ro
  subtitlePreviewContainerWidth.value = el.offsetWidth
  subtitlePreviewContainerHeight.value = el.offsetHeight
}

watch(subtitlePreviewContainerRef, (el) => {
  if (el) startPreviewContainerObserver(el)
}, { immediate: true })

watch(
  [
    selectedSlide,
    () => globalSettings.value.subtitle.fontSize,
    () => globalSettings.value.subtitle.color,
    () => globalSettings.value.subtitle.enableBackground,
    () => globalSettings.value.subtitle.bgColor,
    () => globalSettings.value.subtitle.bgOpacity,
    () => globalSettings.value.subtitle.marginV,
    () => globalSettings.value.subtitle.enableOutline,
    () => globalSettings.value.subtitle.outlineColor,
    () => globalSettings.value.subtitle.outlineWidth,
    () => globalSettings.value.subtitle.outputMode,
    () => globalSettings.value.subtitle.activeWordColor,
    enableSubtitleHighlight,
  ],
  () => {
    queueSubtitleAssRender(80)
  },
  { deep: false, immediate: true },
)

watch(activeTab, async (tab) => {
  if (tab === 'subtitle') {
    await nextTick()
    syncSubtitleAssCanvasSize()
    if (enableSubtitleHighlight.value) startSubtitleHighlightLoop()
    queueSubtitleAssRender(0)
  } else {
    stopSubtitleHighlightLoop()
  }
})

watch(enableSubtitleHighlight, (on) => {
  if (on && activeTab.value === 'subtitle') startSubtitleHighlightLoop()
  else stopSubtitleHighlightLoop()
})

watch(
  () => globalSettings.value.subtitle.outlineWidth,
  (v) => {
    const n = Number(v)
    if (!Number.isFinite(n) || n < 0) globalSettings.value.subtitle.outlineWidth = 0
    else if (n > 10) globalSettings.value.subtitle.outlineWidth = 10
  },
)

watch(
  [
    () => globalSettings.value.subtitle.fontSize,
    () => globalSettings.value.subtitle.color,
    () => globalSettings.value.subtitle.enableBackground,
    () => globalSettings.value.subtitle.bgColor,
    () => globalSettings.value.subtitle.bgOpacity,
    () => globalSettings.value.subtitle.marginV,
    () => globalSettings.value.subtitle.enableOutline,
    () => globalSettings.value.subtitle.outlineColor,
    () => globalSettings.value.subtitle.outlineWidth,
    () => globalSettings.value.subtitle.outputMode,
    () => globalSettings.value.subtitle.activeWordColor,
    enableSubtitleHighlight,
    () => globalSettings.value.tts.speed,
    selectedVoiceKey,
    referenceText,
  ],
  () => schedulePersistRunSettings(),
  { deep: false },
)

watch(
  [subtitlePreviewContainerWidth, subtitlePreviewContainerHeight],
  ([w, h]) => {
    if (activeTab.value !== 'subtitle') return
    if (!w || !h) return
    syncSubtitleAssCanvasSize()
    queueSubtitleAssRender(0)
  },
)

onBeforeUnmount(() => {
  subtitlePreviewResizeObserver.value?.disconnect()
  if (subtitleAssRerenderTimer) clearTimeout(subtitleAssRerenderTimer)
  if (qwenTtsWarmupTimer) clearTimeout(qwenTtsWarmupTimer)
  clearProjectSettingsSaveTimer()
  if (scriptSaveTimer) clearTimeout(scriptSaveTimer)
  stopSubtitleHighlightLoop()
  disposeSubtitleAssEngine()
})

// 字幕字體上限（對應基準寬度 1280px 時 font-size=20px，等比例縮放）
const BASE_CANVAS_WIDTH = 1280
const BASE_SUBTITLE_FONT_SIZE = 20
const computedSubtitleFontSize = computed(() => {
  const containerW = subtitlePreviewContainerWidth.value || 0
  if (containerW < 1) return `${BASE_SUBTITLE_FONT_SIZE}px`
  const scale = containerW / BASE_CANVAS_WIDTH
  const scaledSize = Math.round(BASE_SUBTITLE_FONT_SIZE * scale)
  const clampedSize = Math.max(11, Math.min(scaledSize, 40))
  return `${clampedSize}px`
})

const effectiveSubtitleFontSize = computed(() => {
  const containerW = subtitlePreviewContainerWidth.value || 0
  const scale = containerW < 1 ? 1 : containerW / BASE_CANVAS_WIDTH

  let baseSize = Number(globalSettings.value.subtitle.fontSize || 20)

  return `${Math.max(11, Math.round(baseSize * scale))}px`
})

const subtitleComputedBaseStyle = computed(() => {
  // 共用基礎樣式
  const s = globalSettings.value.subtitle
  const baseColor = s.color || '#ffffff'
  const outlineColor = s.outlineColor || '#000000'
  const outlineWidth = Math.max(0, Number(s.outlineWidth || 2))
  const outlineShadow =
    `-${outlineWidth}px -${outlineWidth}px 0 ${outlineColor}, ` +
    `0 -${outlineWidth}px 0 ${outlineColor}, ` +
    `${outlineWidth}px -${outlineWidth}px 0 ${outlineColor}, ` +
    `-${outlineWidth}px 0 0 ${outlineColor}, ` +
    `${outlineWidth}px 0 0 ${outlineColor}, ` +
    `-${outlineWidth}px ${outlineWidth}px 0 ${outlineColor}, ` +
    `0 ${outlineWidth}px 0 ${outlineColor}, ` +
    `${outlineWidth}px ${outlineWidth}px 0 ${outlineColor}`

  const base = {
    color: baseColor,
    fontSize: effectiveSubtitleFontSize.value,
    position: 'absolute',
    left: '50%',
    bottom: '7.5%',
    transform: 'translateX(-50%)',
    maxWidth: '80%',
    textAlign: 'center',
    lineHeight: '1.5',
    userSelect: 'none',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-all',
    zIndex: 10
  }

  // 根據選擇注入樣式
  if (subtitleStyle.value === 'bg-dark') {
    const alpha = Math.max(0, Math.min(100, Number(s.bgOpacity))) / 100
    base.background = `rgba(0,0,0,${alpha.toFixed(2)})`
    base.padding = '4px 12px'
    base.borderRadius = '8px'
    base.boxShadow = '0 2px 8px rgba(0,0,0,0.35)'
    base.border = '1px solid rgba(255,255,255,0.08)'
  } else if (subtitleStyle.value === 'stroke-dark') {
    base.background = 'transparent'
    base.fontWeight = 'bold'
    base.textShadow = `${outlineShadow}, 0 0 6px rgba(0,0,0,0.85)`
  } else if (subtitleStyle.value === 'bg-gray') {
    const alpha = Math.max(0, Math.min(100, Number(s.bgOpacity))) / 100
    base.background = `rgba(128,128,128,${alpha.toFixed(2)})`
    base.padding = '4px 12px'
    base.borderRadius = '8px'
    base.boxShadow = '0 2px 8px rgba(0,0,0,0.35)'
    base.border = '1px solid rgba(255,255,255,0.08)'
  }

  if (s.enableOutline && subtitleStyle.value !== 'stroke-dark') {
    base.textShadow = `${outlineShadow}, 0 1px 8px rgba(0,0,0,0.55)`
  }

  return base
})

const subtitleComputedActiveStyle = computed(() => {
  const s = globalSettings.value.subtitle
  const hlColor = s.activeWordColor || '#fbbf24'
  const outlineColor = s.outlineColor || '#000000'
  const outlineWidth = Math.max(0, Number(s.outlineWidth || 2))
  const outlineShadow =
    `-${outlineWidth}px -${outlineWidth}px 0 ${outlineColor}, ` +
    `0 -${outlineWidth}px 0 ${outlineColor}, ` +
    `${outlineWidth}px -${outlineWidth}px 0 ${outlineColor}, ` +
    `-${outlineWidth}px 0 0 ${outlineColor}, ` +
    `${outlineWidth}px 0 0 ${outlineColor}, ` +
    `-${outlineWidth}px ${outlineWidth}px 0 ${outlineColor}, ` +
    `0 ${outlineWidth}px 0 ${outlineColor}, ` +
    `${outlineWidth}px ${outlineWidth}px 0 ${outlineColor}`
  let shadow = `0 0 4px ${hlColor}`
  if (subtitleStyle.value === 'stroke-dark') {
    shadow = `${outlineShadow}, 0 0 10px ${hlColor}`
  } else if (subtitleStyle.value === 'stroke-light') {
    shadow = `-2px -2px 0 #9ca3af, 2px -2px 0 #9ca3af, -2px 2px 0 #9ca3af, 2px 2px 0 #9ca3af, 0 0 10px ${hlColor}`
  } else if (s.enableOutline) {
    shadow = `${outlineShadow}, 0 0 6px ${hlColor}`
  }
  return {
    color: hlColor,
    textShadow: shadow,
    transition: 'color 0.1s ease-in'
  }
})

const subtitleAssPreviewText = computed(() => {
  return '這是一段測試文字，用來展示字幕效果'
})
const subtitleFastChars = computed(() => Array.from(subtitleAssPreviewText.value || ''))
const subtitleHighlightLoopIndex = ref(-1)
let subtitleHighlightLoopTimer = null
const subtitleHighlightStepMs = 350
const subtitleHighlightPauseMs = 2000
const isCommaChar = (ch) => ch === '，' || ch === ','
const subtitleHighlightIndices = computed(() =>
  subtitleFastChars.value
    .map((ch, i) => ({ ch, i }))
    .filter(({ ch }) => !isCommaChar(ch))
    .map(({ i }) => i)
)

const stopSubtitleHighlightLoop = () => {
  if (subtitleHighlightLoopTimer) {
    clearTimeout(subtitleHighlightLoopTimer)
    subtitleHighlightLoopTimer = null
  }
  subtitleHighlightLoopIndex.value = -1
}

const scheduleSubtitleHighlightStep = (seq, idxPos) => {
  if (!enableSubtitleHighlight.value || activeTab.value !== 'subtitle') return
  if (!seq.length) return
  subtitleHighlightLoopIndex.value = seq[idxPos]
  const isLast = idxPos >= seq.length - 1
  const nextDelay = subtitleHighlightStepMs
  subtitleHighlightLoopTimer = setTimeout(() => {
    if (!enableSubtitleHighlight.value || activeTab.value !== 'subtitle') return
    if (isLast) {
      subtitleHighlightLoopIndex.value = -1
      subtitleHighlightLoopTimer = setTimeout(() => {
        if (!enableSubtitleHighlight.value || activeTab.value !== 'subtitle') return
        scheduleSubtitleHighlightStep(seq, 0)
      }, subtitleHighlightPauseMs)
    } else {
      scheduleSubtitleHighlightStep(seq, idxPos + 1)
    }
  }, nextDelay)
}

const startSubtitleHighlightLoop = () => {
  stopSubtitleHighlightLoop()
  const seq = subtitleHighlightIndices.value
  if (!seq.length) return
  scheduleSubtitleHighlightStep(seq, 0)
}

const subtitleFastOverlayStyle = computed(() => {
  const mv = Math.max(0, Number(globalSettings.value.subtitle.marginV ?? 90))
  const bottomRatio = mv / 1080
  const bottomPx = Math.max(8, Math.round((subtitlePreviewContainerHeight.value || 720) * bottomRatio))
  return {
    bottom: `${bottomPx}px`,
  }
})
const subtitleFastBoxStyle = computed(() => {
  const s = globalSettings.value.subtitle
  const scale = Math.max(0.1, (subtitlePreviewContainerWidth.value || 1280) / 1920)
  const fz = Math.max(11, Math.round(Number(s.fontSize ?? 52) * scale))
  const bgOpacity = Math.max(0, Math.min(100, Number(s.bgOpacity ?? 55))) / 100
  const bgColor = s.bgColor || '#000000'
  const bgEnabled = Boolean(s.enableBackground)
  const outlineColor = s.outlineColor || '#000000'
  const outlineW = s.enableOutline ? Math.max(0, Number(s.outlineWidth ?? 2)) : 0
  const webkitStroke = outlineW > 0 ? `${outlineW}px ${outlineColor}` : '0 transparent'
  return {
    fontSize: `${fz}px`,
    color: s.color || '#ffffff',
    background: (!bgEnabled || bgOpacity <= 0) ? 'transparent' : `rgba(${hexToRgb(bgColor)},${bgOpacity.toFixed(2)})`,
    WebkitTextStroke: webkitStroke,
    paintOrder: 'stroke fill',
    textShadow: outlineW > 0 ? `0 0 ${Math.max(1, Math.round(outlineW * 0.35))}px ${outlineColor}` : 'none',
  }
})
const subtitleFastActiveCharStyle = computed(() => ({
  color: globalSettings.value.subtitle.activeWordColor || '#facc15',
}))

const ensureSubtitleAssEngine = async () => {
  if (globalThis.SubtitlesOctopus) return globalThis.SubtitlesOctopus
  if (subtitleAssScriptPromise) return subtitleAssScriptPromise
  subtitleAssScriptPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = '/vendor/subtitles-octopus.js'
    s.async = true
    s.onload = () => resolve(globalThis.SubtitlesOctopus)
    s.onerror = () => reject(new Error('SubtitlesOctopus script load failed'))
    document.head.appendChild(s)
  })
  return subtitleAssScriptPromise
}

const hexToAssBgr = (hex) => {
  const raw = String(hex || '#000000').replace('#', '')
  const v = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw.padEnd(6, '0')
  const r = parseInt(v.slice(0, 2), 16) || 0
  const g = parseInt(v.slice(2, 4), 16) || 0
  const b = parseInt(v.slice(4, 6), 16) || 0
  return `&H${b.toString(16).padStart(2, '0').toUpperCase()}${g.toString(16).padStart(2, '0').toUpperCase()}${r.toString(16).padStart(2, '0').toUpperCase()}&`
}

const hexToRgb = (hex) => {
  const raw = String(hex || '#000000').replace('#', '')
  const v = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw.padEnd(6, '0')
  const r = parseInt(v.slice(0, 2), 16) || 0
  const g = parseInt(v.slice(2, 4), 16) || 0
  const b = parseInt(v.slice(4, 6), 16) || 0
  return `${r},${g},${b}`
}

const buildSubtitleAssPreview = () => {
  const s = globalSettings.value.subtitle
  const primary = hexToAssBgr(s.color)
  const hi = hexToAssBgr(s.activeWordColor || '#facc15')
  const ol = hexToAssBgr(s.outlineColor || '#000000')
  const bgOpacity = Math.max(0, Math.min(100, Number(s.bgOpacity ?? 55)))
  const alpha = Math.max(0, Math.min(255, Math.round((100 - bgOpacity) * 255 / 100)))
  const bgColorAss = hexToAssBgr(s.bgColor || '#000000').replace(/^&H|&$/g, '')
  const bg = `&H${alpha.toString(16).padStart(2, '0').toUpperCase()}${bgColorAss}`
  const outlineW = s.enableOutline ? Math.max(0, Number(s.outlineWidth ?? 2)) : 0
  const chars = Array.from(subtitleAssPreviewText.value)
  const renderedCore = chars.map((ch, i) => (enableSubtitleHighlight.value && i === subtitleHighlightLoopIndex.value ? `{\\1c${hi}}${ch}{\\1c${primary}}` : ch)).join('')
  const PAD_X = 0.5
  const PAD_Y = 0.1
  const sidePx = PAD_X * 12
  const padTopBottomFs = Math.max(0, Math.round(Math.max(11, Number(s.fontSize ?? 36)) * PAD_Y))
  const sidePad = sidePx > 0 ? `{\\fsp${sidePx}}\\h{\\fsp0}` : ''
  const topPad = padTopBottomFs > 0 ? `{\\fs${padTopBottomFs}}　{\\rDefault}\\N` : ''
  const bottomPad = padTopBottomFs > 0 ? `\\N{\\fs${padTopBottomFs}}　{\\rDefault}` : ''
  const rendered = `${topPad}${sidePad}${renderedCore}${sidePad}${bottomPad}`

  return `[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK TC,${Math.max(11, Number(s.fontSize ?? 52))},${primary},${primary},${ol},${bg},-1,0,0,0,100,100,0,0,4,${outlineW},${(s.enableBackground && bgOpacity > 0) ? 1 : 0},2,20,20,${Math.max(0, Number(s.marginV ?? 90))},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:30.00,Default,,0,0,0,,${rendered}
`
}

const renderSubtitleAssPreview = async () => {
  if (USE_FAST_SUBTITLE_PREVIEW) return
  if (activeTab.value !== 'subtitle') return
  if (!subtitleAssCanvasRef.value) return
  try {
    syncSubtitleAssCanvasSize()
    const Oct = await ensureSubtitleAssEngine()
    const ass = buildSubtitleAssPreview()
    const contentChanged = ass !== subtitleAssLastContent
    if (!subtitleAssEngine) {
      subtitleAssEngine = new Oct({
        canvas: subtitleAssCanvasRef.value,
        subContent: ass,
        workerUrl: '/vendor/subtitles-octopus-worker.js',
        fallbackFont: '/vendor/NotoSansCJKtc-Regular.otf',
      })
      subtitleAssLastContent = ass
    } else if (contentChanged && typeof subtitleAssEngine.setTrackByContent === 'function') {
      subtitleAssEngine.setTrackByContent(ass)
      subtitleAssLastContent = ass
    } else if (contentChanged && typeof subtitleAssEngine.setTrack === 'function') {
      subtitleAssEngine.setTrack(ass)
      subtitleAssLastContent = ass
    }
    subtitleAssEngine.setCurrentTime?.(1.0)
  } catch (e) {
    console.warn('ASS subtitle preview render failed:', e)
  }
}

const disposeSubtitleAssEngine = () => {
  if (subtitleAssEngine) {
    try { subtitleAssEngine.dispose() } catch {}
    subtitleAssEngine = null
  }
  subtitleAssLastContent = ''
}

function queueSubtitleAssRender(delayMs = 80) {
  if (subtitleAssRerenderTimer) clearTimeout(subtitleAssRerenderTimer)
  subtitleAssRerenderTimer = setTimeout(() => {
    renderSubtitleAssPreview()
  }, delayMs)
}

const syncSubtitleAssCanvasSize = () => {
  const canvas = subtitleAssCanvasRef.value
  const host = subtitlePreviewContainerRef.value
  if (!canvas || !host) return false
  const dpr = Math.max(1, window.devicePixelRatio || 1)
  const w = Math.max(1, Math.round(host.clientWidth * dpr))
  const h = Math.max(1, Math.round(host.clientHeight * dpr))
  const changed = w !== subtitleAssCanvasPixelW || h !== subtitleAssCanvasPixelH
  if (changed) {
    subtitleAssCanvasPixelW = w
    subtitleAssCanvasPixelH = h
    canvas.width = w
    canvas.height = h
    if (subtitleAssEngine && typeof subtitleAssEngine.resize === 'function') {
      try { subtitleAssEngine.resize(w, h) } catch {}
    }
  }
  return changed
}

const resetMessages = () => {
  statusMessage.value = ''
  errorMessage.value = ''
  renderMessage.value = ''
}

const refreshSidebarRuns = () => {
  clearEndpointCache('/api/video-runs')
  emitter.emit('refresh-video-runs')
}

const resetEphemeralUrls = () => {
  tempObjectUrls.value.forEach((url) => {
    if (typeof url === 'string' && url.startsWith('blob:')) {
      try { URL.revokeObjectURL(url) } catch {}
    }
  })
  tempObjectUrls.value = []
  if (cloneAudioUrl.value?.startsWith('blob:')) {
    try { URL.revokeObjectURL(cloneAudioUrl.value) } catch {}
  }
  if (subtitleAudioUrl.value?.startsWith('blob:')) {
    try { URL.revokeObjectURL(subtitleAudioUrl.value) } catch {}
  }
  if (ttsPreviewUrl.value?.startsWith('blob:')) {
    try { URL.revokeObjectURL(ttsPreviewUrl.value) } catch {}
  }
  cloneAudioUrl.value = ''
  subtitleAudioUrl.value = ''
  ttsPreviewUrl.value = ''
}

const clearTtsPreviewState = () => {
  if (ttsPreviewUrl.value?.startsWith('blob:')) {
    try { URL.revokeObjectURL(ttsPreviewUrl.value) } catch {}
  }
  ttsPreviewText.value = ''
  ttsPreviewUrl.value = ''
  ttsGenerating.value = false
  ttsError.value = ''
  asrFilling.value = false
}

const resetVoiceToDefault = async () => {
  await _loadManifest()
  const firstKey = Object.keys(presetManifest.value)[0] || ''
  selectedVoiceKey.value = firstKey
  referenceText.value = ''
  cloneAudioFile.value = null
  cloneAudioUrl.value = ''
  presetLoadError.value = false
  if (firstKey) await loadPresetVoice(firstKey)
}

const resetProjectState = async ({ resetPdf = true } = {}) => {
  currentRunId.value = null
  runManifest.value = null
  pdfId.value = null
  selectedVariantIds.value = {}
  selectedSlideIndex.value = 0
  slides.value = []
  renderedPageVideos.value = {}
  subtitleAudioFile.value = null
  subtitleAudioUrl.value = ''
  subtitleAlignText.value = ''
  subtitleAligning.value = false
  subtitleAligningStage.value = ''
  subtitleAlignError.value = ''
  subtitleAlignWarning.value = ''
  subtitleAlignedSegments.value = []
  subtitleAlignBackend.value = ''
  subtitleAlignSrt.value = ''
  subtitleDemoCurrentTime.value = 0
  subtitleDemoDuration.value = 0
  subtitleDemoPlaying.value = false
  stopSubtitleClock()
  enableSubtitleHighlight.value = false
  subtitleStyle.value = 'bg-dark'
  subtitleTestText.value = ''
  clearTtsPreviewState()
  contentLanguage.value = 'zh'
  subtitleSource.value = 'zh'
  userProvidedScript.value = ''
  globalSettings.value = createDefaultGlobalSettings()
  if (resetPdf) {
    pdfFile.value = null
    if (fileInput.value) fileInput.value.value = ''
  }
  if (subtitleAudioInput.value) subtitleAudioInput.value.value = ''
  await resetVoiceToDefault()
}

const isPdfFile = (file) => {
  if (!file) return false
  return file.type === 'application/pdf' || /\.pdf$/i.test(file.name)
}

const splitUserScriptToPages = (rawText, pageCount) => {
  const result = Array(pageCount).fill('')
  const text = String(rawText || '').trim()
  if (!text) return result

  // Preferred: #PAGE_001# ... #END_PAGE_001#; also accepts 第1頁/Page 1/Slide 1.
  const regex = /(?:^|\n)\s*(?:#?\s*PAGE[_\-\s]*0*(\d+)\s*#?|第\s*(\d+)\s*頁\s*[:：]|(?:Page|Slide)\s*(\d+)\s*[:：])/gi
  const matches = []
  let match
  while ((match = regex.exec(text)) !== null) {
    matches.push({ page: Number(match[1] || match[2] || match[3]), index: match.index, markerLen: match[0].length })
  }

  if (matches.length > 0) {
    for (let i = 0; i < matches.length; i += 1) {
      const current = matches[i]
      const next = matches[i + 1]
      const start = current.index + current.markerLen
      const end = next ? next.index : text.length
      const pageIdx = current.page - 1
      if (pageIdx >= 0 && pageIdx < pageCount) {
        result[pageIdx] = text
          .slice(start, end)
          .replace(/^\s*#?\s*(?:END[_\-\s]*PAGE|ENDPAGE)[_\-\s]*0*\d+\s*#?\s*$/gim, '')
          .trim()
      }
    }
    return result
  }

  // Fallback: split by blank lines
  const chunks = text
    .split(/\n\s*\n+/)
    .map((x) => x.trim())
    .filter(Boolean)
  for (let i = 0; i < pageCount; i += 1) {
    result[i] = chunks[i] || ''
  }
  return result
}

const setPdf = (file) => {
  if (!isPdfFile(file)) {
    errorMessage.value = '請選擇 PDF 檔案'
    return
  }
  pdfFile.value = file
}

const onFileChange = (event) => {
  resetMessages()
  setPdf(event.target.files?.[0])
}

const isDragOver = ref(false)

const onDragOver = (e) => {
  e.dataTransfer.dropEffect = 'copy'
  isDragOver.value = true
}

const onDragLeave = (e) => {
  // Only clear drag state when leaving the container itself, not entering a child
  const relatedTarget = e.relatedTarget
  const uploadEl = e.currentTarget
  if (!relatedTarget || !uploadEl.contains(relatedTarget)) {
    isDragOver.value = false
  }
}

const onDrop = (event) => {
  isDragOver.value = false
  resetMessages()
  setPdf(event.dataTransfer?.files?.[0])
}

const createSlideModels = (texts) => texts.map((text, idx) => ({
  id: `${idx + 1}`,
  scriptText: text || '',
  thumbnailUrl: '',
  previewUrl: '',
  thumbnailError: '',
  previewError: '',
}))

let imageSession = 0
const activeImageControllers = new Set()
const resetImageSession = () => {
  imageSession += 1
  for (const controller of activeImageControllers) controller.abort()
  activeImageControllers.clear()
}
const queueImage = createImageQueue(3)
let imageComponentMounted = true

const imageSessionIsCurrent = (runId, session) => (
  imageComponentMounted && currentRunId.value === runId && imageSession === session
)

const waitForImageDecode = (url, timeoutMs = 10000) => new Promise((resolve, reject) => {
  const image = new Image()
  let settled = false
  const finish = (error = null) => {
    if (settled) return
    settled = true
    clearTimeout(timer)
    image.onload = null
    image.onerror = null
    if (error) reject(error)
    else resolve()
  }
  const timer = setTimeout(() => finish(new Error('image decode timeout')), timeoutMs)
  image.onload = () => {
    // decode() catches decode failures that occur after load in some browsers.
    if (typeof image.decode === 'function') image.decode().then(() => finish()).catch((err) => finish(err))
    else finish()
  }
  image.onerror = () => finish(new Error('image decode failed'))
  image.src = url
})

const enqueueRunImage = (idx, runId, kind, { priority = false, session = imageSession } = {}) => {
  if (!runId || !slides.value[idx]) return Promise.resolve()
  const key = `${runId}:${idx}:${kind}:${session}`
  return queueImage(key, async () => {
    const slide = slides.value[idx]
    if (!slide || (kind === 'thumbnail' ? slide.thumbnailUrl : slide.previewUrl)) return
    if (!imageSessionIsCurrent(runId, session)) return
    let objectUrl = ''
    try {
      const endpoint = kind === 'thumbnail'
        ? `${getApiEndpoint(`/api/video-runs/${encodeURIComponent(runId)}/thumbnail`)}?page=${idx + 1}`
        : getApiEndpoint(`/api/video-runs/${encodeURIComponent(runId)}/pages/${idx}/image`)
      const controller = new AbortController()
      activeImageControllers.add(controller)
      const timeout = setTimeout(() => controller.abort(), 15000)
      let blob
      try {
        const res = await fetch(endpoint, { signal: controller.signal })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        blob = await res.blob()
      } finally {
        clearTimeout(timeout)
        activeImageControllers.delete(controller)
      }
      objectUrl = URL.createObjectURL(blob)
      if (!imageSessionIsCurrent(runId, session)) {
        URL.revokeObjectURL(objectUrl)
        return
      }
      await waitForImageDecode(objectUrl)
      if (!imageSessionIsCurrent(runId, session)) {
        URL.revokeObjectURL(objectUrl)
        return
      }
      tempObjectUrls.value.push(objectUrl)
      if (kind === 'thumbnail') slide.thumbnailUrl = objectUrl
      else slide.previewUrl = objectUrl
      slide[`${kind}Error`] = ''
    } catch (err) {
      if (objectUrl) URL.revokeObjectURL(objectUrl)
      if (imageSessionIsCurrent(runId, session) && slides.value[idx]) {
        slides.value[idx][`${kind}Error`] = `第 ${idx + 1} 頁${kind === 'thumbnail' ? '縮圖' : '預覽圖'}載入失敗，請重試。`
      }
      console.warn(`[RunImage:${kind}] load failed:`, err)
    }
  }, priority)
}

const prepareInitialRunImages = (runId, session) => Promise.all([
  ...(slides.value.length ? [enqueueRunImage(0, runId, 'preview', { session, priority: true })] : []),
  ...slides.value.map((_, idx) => enqueueRunImage(idx, runId, 'thumbnail', { session })),
])

const preloadRemainingRunImages = (runId, session) => {
  slides.value.slice(1).forEach((_, idx) => {
    enqueueRunImage(idx + 1, runId, 'preview', { session }).catch(() => {})
  })
}

const fetchRunThumbnail = async (idx, runId) => {
  return enqueueRunImage(idx, runId, 'thumbnail')
}

const fetchRunPreview = async (idx, runId) => {
  return enqueueRunImage(idx, runId, 'preview', { priority: true })
}

const persistRunScriptsNow = async () => {
  if (!currentRunId.value || stage.value !== 'workspace' || suppressScriptSave.value) return
  const scripts = slides.value.map((slide) => String(slide?.scriptText || ''))
  try {
    const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/scripts`), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scripts }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || `講稿保存失敗 (${res.status})`)
    runManifest.value = data
  } catch (err) {
    console.warn('[VideoRun] script save failed:', err)
    renderMessage.value = err.message || '講稿保存失敗'
  }
}

const schedulePersistRunScripts = () => {
  if (!currentRunId.value || stage.value !== 'workspace' || suppressScriptSave.value) return
  if (scriptSaveTimer) clearTimeout(scriptSaveTimer)
  scriptSaveTimer = setTimeout(() => {
    scriptSaveTimer = null
    persistRunScriptsNow()
  }, 700)
}

const applyGeneratedScripts = (scripts) => {
  if (!Array.isArray(scripts)) return
  suppressScriptSave.value = true
  scripts.forEach((text, idx) => {
    if (slides.value[idx]) slides.value[idx].scriptText = String(text || '')
  })
  nextTick(() => {
    suppressScriptSave.value = false
  })
}

const generateScriptsForPages = async (pageIndexes, options = {}) => {
  const requireConfirm = options?.requireConfirm !== false
  const throwOnError = options?.throwOnError === true
  if (!currentRunId.value || scriptGenerating.value) return
  const isAll = pageIndexes?.length !== 1
  const scope = isAll ? 'all' : 'current'
  const normalizedPages = isAll ? [] : [selectedSlideIndex.value]
  const message = isAll
    ? '這會呼叫 LLM API 重新填寫全部頁面的講稿，並覆蓋目前所有頁面的既有講稿內容。確定要繼續？'
    : `這會呼叫 LLM API 重新填寫第 ${selectedSlideIndex.value + 1} 頁講稿，並覆蓋此頁既有講稿內容。確定要繼續？`
  if (requireConfirm && !window.confirm(message)) return
  scriptGenerating.value = true
  renderMessage.value = ''
  try {
    const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/scripts/generate`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scope,
        source: 'pdf',
        pages: normalizedPages,
        language: contentLanguage.value || 'zh',
        overwrite: true,
      }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || `LLM 講稿生成失敗 (${res.status})`)
    runManifest.value = data.run || runManifest.value
    applyGeneratedScripts(data.scripts || [])
    const updatedCount = Array.isArray(data?.updated_pages) ? data.updated_pages.length : 0
    const requestedCount = Number(data?.text_stats?.requested_pages || 0)
    if (updatedCount === 0) {
      throw new Error(
        requestedCount > 0
          ? `LLM 本次未成功生成任何頁面（0/${requestedCount}）。請改用可讀 PDF/圖片的模型，或重試一次。`
          : 'LLM 本次未成功生成任何頁面。'
      )
    }
    const skipped = Array.isArray(data?.skipped_empty_pages) ? data.skipped_empty_pages.length : 0
    const baseMsg = pageIndexes?.length === 1 ? '已重新填寫本頁講稿。' : '已重新填寫全部頁面講稿。'
    statusMessage.value = skipped > 0 ? `${baseMsg}（略過 ${skipped} 頁無可抽取文字的頁面）` : baseMsg
    refreshSidebarRuns()
  } catch (err) {
    const msg = String(err?.message || '')
    if (msg.includes('API key not valid') || msg.includes('API_KEY_INVALID')) {
      errorMessage.value = 'LLM API key 無效，請檢查 backend/.env 的 api_key 是否為有效金鑰。'
    } else {
      errorMessage.value = msg || 'LLM 講稿生成失敗'
    }
    if (throwOnError) throw err
  } finally {
    scriptGenerating.value = false
  }
}

const generateScriptForCurrentPage = () => {
  generateScriptsForPages([selectedSlideIndex.value])
}

const generateScriptsForAllPages = () => {
  generateScriptsForPages(slides.value.map((_, idx) => idx))
}

const handleSlideIndexChange = (idx) => {
  isMergedPreviewSelected.value = false
  selectedSlideIndex.value = Number(idx) || 0
}

const selectMergedPreviewPage = () => {
  if (!hasMergedPreview.value) return
  if (!mergedPreviewVideoUrl.value && selectedExportVariantId.value) {
    mergedPreviewVideoUrl.value = exportVideoUrl(selectedExportVariantId.value)
  }
  mergedPreviewThumbnailUrl.value = mergedPreviewThumbnailUrl.value || slides.value[0]?.thumbnailUrl || slides.value[0]?.previewUrl || ''
  isMergedPreviewSelected.value = true
  activeTab.value = 'preview'
}

const selectMergedExportVariant = async (variantId) => {
  if (!variantId) return
  await selectExportVariant(variantId)
  mergedPreviewVideoUrl.value = exportVideoUrl(variantId)
  mergedPreviewThumbnailUrl.value = mergedPreviewThumbnailUrl.value || slides.value[0]?.thumbnailUrl || slides.value[0]?.previewUrl || ''
  isMergedPreviewSelected.value = true
}

const deleteMergedExportVariant = async (variantId) => {
  if (!variantId) return
  await deleteExportVariant(variantId)
  const next = selectedExportVariantId.value
  if (next) {
    mergedPreviewVideoUrl.value = exportVideoUrl(next)
    mergedPreviewThumbnailUrl.value = mergedPreviewThumbnailUrl.value || slides.value[0]?.thumbnailUrl || slides.value[0]?.previewUrl || ''
    isMergedPreviewSelected.value = true
  } else {
    if (mergedPreviewVideoUrl.value && mergedPreviewVideoUrl.value.startsWith('blob:')) {
      try { URL.revokeObjectURL(mergedPreviewVideoUrl.value) } catch {}
    }
    mergedPreviewVideoUrl.value = ''
    mergedPreviewThumbnailUrl.value = ''
    isMergedPreviewSelected.value = false
  }
}

watch(
  () => slides.value.map((slide) => String(slide?.scriptText || '')),
  () => schedulePersistRunScripts(),
)

watch(
  [selectedSlideIndex, currentRunId],
  ([idx, runId]) => {
    if (isMergedPreviewSelected.value) return
    if (stage.value === 'workspace' && runId) fetchRunPreview(idx, runId)
  },
)

watch(activeTab, (tab) => {
  if (tab !== 'preview') isMergedPreviewSelected.value = false
})

const handleShellProjectSelect = async (project) => {
  if (!project?.is_video_run || !project?.run_id) return
  resetImageSession()
  const session = imageSession
  openingProject.value = true
  resetMessages()
  clearProjectSettingsSaveTimer()
  if (scriptSaveTimer) {
    clearTimeout(scriptSaveTimer)
    scriptSaveTimer = null
  }
  resetEphemeralUrls()
  clearTtsPreviewState()
  if (mergedPreviewVideoUrl.value && mergedPreviewVideoUrl.value.startsWith('blob:')) {
    try { URL.revokeObjectURL(mergedPreviewVideoUrl.value) } catch {}
  }
  mergedPreviewVideoUrl.value = ''
  mergedPreviewThumbnailUrl.value = ''
  isMergedPreviewSelected.value = false
  suppressScriptSave.value = true
  suppressProjectSettingsSave.value = true
  try {
    globalSettings.value = createDefaultGlobalSettings()
    enableSubtitleHighlight.value = false
    selectedVariantIds.value = {}
    renderedPageVideos.value = {}
    await resetVoiceToDefault()
    if (session !== imageSession) return
    currentRunId.value = project.run_id
    const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(project.run_id)}`))
    const manifest = await res.json().catch(() => ({}))
    if (session !== imageSession) return
    if (!res.ok) throw new Error(manifest?.detail || `載入 run 失敗 (${res.status})`)
    runManifest.value = manifest
    pdfId.value = manifest.pdf_id || null
    const pages = Array.isArray(manifest.pages) ? manifest.pages : []
    slides.value = pages.map((page, idx) => ({
      id: `${idx + 1}`,
      scriptText: page.script || '',
      thumbnailUrl: '',
      previewUrl: '',
      thumbnailError: '',
      previewError: '',
    }))
    selectedSlideIndex.value = 0
    selectedVariantIds.value = {}
    renderedPageVideos.value = {}
    pages.forEach((page, idx) => {
      const selected = page.selected_variant_id || page.variants?.[page.variants.length - 1]?.variant_id || ''
      if (selected) {
        selectedVariantIds.value[idx] = selected
        renderedPageVideos.value[idx] = variantVideoUrl(idx, selected)
      }
    })
    const exportList = Array.isArray(manifest?.exports?.variants) ? manifest.exports.variants : []
    const selectedExport = manifest?.exports?.selected_variant_id || exportList[exportList.length - 1]?.variant_id || ''
    if (selectedExport) {
      mergedPreviewVideoUrl.value = exportVideoUrl(selectedExport)
      mergedPreviewThumbnailUrl.value = slides.value[0]?.thumbnailUrl || slides.value[0]?.previewUrl || ''
    }
    await applyProjectSettingsToUi(manifest)
    if (session !== imageSession) return
    statusMessage.value = `已載入 ${manifest.display_name || manifest.original_filename || manifest.run_id}。`
    await prepareInitialRunImages(project.run_id, session)
    if (session !== imageSession || currentRunId.value !== project.run_id) return
    stage.value = 'workspace'
    activeTab.value = 'preview'
    preloadRemainingRunImages(project.run_id, session)
    if (hasMergedPreview.value) {
      mergedPreviewThumbnailUrl.value = mergedPreviewThumbnailUrl.value || slides.value[0]?.thumbnailUrl || slides.value[0]?.previewUrl || ''
    }
    reattachActiveBatchJob().catch((err) => {
      console.warn('[BatchJob] reattach failed:', err)
    })
  } catch (err) {
    if (session === imageSession) errorMessage.value = err.message || '載入 run 失敗'
  } finally {
    if (session === imageSession) {
      openingProject.value = false
      await nextTick()
      suppressScriptSave.value = false
      suppressProjectSettingsSave.value = false
    }
  }
}

const handleShellProjectDeleted = async (runId) => {
  if (!runId || runId !== currentRunId.value) return
  await backToUpload()
}

const handleUpload = async () => {
  if (!pdfFile.value) return
  resetMessages()
  uploading.value = true
  uploadPhase.value = '上傳 PDF 中...'
  try {
    const effectiveLanguage = subtitleSource.value === 'en' ? 'en' : 'zh'
    contentLanguage.value = effectiveLanguage

    const formData = new FormData()
    formData.append('file', pdfFile.value)
    formData.append('content_language', effectiveLanguage)
    formData.append('voice', globalSettings.value.tts.voice)
    formData.append('subtitle_source', subtitleSource.value)
    if (subtitleSource.value === 'user_input') {
      formData.append('user_script', String(userProvidedScript.value || ''))
    }
    const shouldSkipLlm = subtitleSource.value === 'none' || subtitleSource.value === 'user_input'
    formData.append('skip_llm', shouldSkipLlm ? 'true' : 'false')

    const res = await fetch(getApiEndpoint(API_ENDPOINTS.VIDEO_ABSTRACT), {
      method: 'POST',
      body: formData,
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || `上傳失敗 (${res.status})`)

    const runId = typeof data?.run_id === 'string' ? data.run_id.trim() : ''
    if (!runId) throw new Error('上傳失敗：後端未回傳專案 run_id，請確認後端支援專案工作流程後重試。')
    pdfId.value = data?.pdf_id || null
    currentRunId.value = runId
    resetImageSession()
    const session = imageSession
    runManifest.value = data?.run || null
    selectedVariantIds.value = {}
    const returnedTexts = Array.isArray(data?.texts) ? data.texts : []
    let finalTexts = returnedTexts

    if (subtitleSource.value === 'none') {
      finalTexts = returnedTexts.map(() => '')
    } else if (subtitleSource.value === 'user_input' && returnedTexts.every((x) => !String(x || '').trim())) {
      finalTexts = splitUserScriptToPages(userProvidedScript.value, returnedTexts.length)
    }

    suppressScriptSave.value = true
    slides.value = createSlideModels(finalTexts)
    selectedSlideIndex.value = 0
    renderedPageVideos.value = {}

    const shouldAutoGenerate = (
      (subtitleSource.value === 'zh' || subtitleSource.value === 'en')
      && data?.model_services_skipped !== true
    )

    refreshSidebarRuns()
    uploadPhase.value = shouldAutoGenerate ? '等待 LLM 回應...' : '載入專案中...'
    await refreshRunManifest()
    await nextTick()
    suppressScriptSave.value = false
    // subtitle source zh/en: trigger initial script generation via the same
    // direct-PDF pipeline used by manual "fill all", avoiding old text-extract path.
    if (shouldAutoGenerate) {
      await generateScriptsForPages(slides.value.map((_, idx) => idx), { requireConfirm: false, throwOnError: true })
    }
    await prepareInitialRunImages(currentRunId.value, session)
    if (session !== imageSession) return
    stage.value = 'workspace'
    activeTab.value = 'script'
    preloadRemainingRunImages(currentRunId.value, session)
    statusMessage.value = data?.model_services_skipped === true
      ? `上傳成功，共 ${slides.value.length} 頁；目前為基本前後端模式，已跳過 AI 講稿生成。`
      : `上傳成功，共 ${slides.value.length} 頁。`
  } catch (err) {
    errorMessage.value = err.message || '上傳失敗'
  } finally {
    if (stage.value !== 'workspace') suppressScriptSave.value = false
    uploading.value = false
    uploadPhase.value = ''
  }
}

const fileToBase64 = (file) => new Promise((resolve, reject) => {
  if (!file) return resolve(null)
  const reader = new FileReader()
  reader.onload = () => {
    const full = String(reader.result || '')
    const idx = full.indexOf(',')
    resolve(idx >= 0 ? full.slice(idx + 1) : full)
  }
  reader.onerror = () => reject(new Error('音檔讀取失敗'))
  reader.readAsDataURL(file)
})

const playVoicePreview = () => {
  const sample = contentLanguage.value === 'en'
    ? 'Voice preview. This is a sample sentence.'
    : '音色試聽，這是一段測試語音。'
  if (!('speechSynthesis' in window)) {
    statusMessage.value = '目前瀏覽器不支援內建語音試聽。'
    return
  }
  const utterance = new SpeechSynthesisUtterance(sample)
  utterance.lang = contentLanguage.value === 'en' ? 'en-US' : 'zh-TW'
  utterance.rate = Number(globalSettings.value.tts.speed) || 1.0
  window.speechSynthesis.cancel()
  window.speechSynthesis.speak(utterance)
  statusMessage.value = '已觸發本機語音試聽。'
}

const onCloneFileChange = (event) => {
  const file = event.target.files?.[0] || null
  cloneAudioFile.value = file
  if (cloneAudioUrl.value) {
    URL.revokeObjectURL(cloneAudioUrl.value)
    cloneAudioUrl.value = ''
  }
  if (file) {
    const objectUrl = URL.createObjectURL(file)
    cloneAudioUrl.value = objectUrl
    tempObjectUrls.value.push(objectUrl)
    schedulePersistRunSettings({ includeReferenceAudio: true, delay: 200 })
    scheduleQwenTtsWarmup()
  }
}

const onRefAudioDrop = (event) => {
  const file = event.dataTransfer.files?.[0] || null
  if (!file) return
  cloneAudioFile.value = file
  if (cloneAudioUrl.value) URL.revokeObjectURL(cloneAudioUrl.value)
  const objectUrl = URL.createObjectURL(file)
  cloneAudioUrl.value = objectUrl
  tempObjectUrls.value.push(objectUrl)
  schedulePersistRunSettings({ includeReferenceAudio: true, delay: 200 })
  scheduleQwenTtsWarmup()
}

const formatAlignTime = (sec) => {
  const n = Number(sec) || 0
  const m = Math.floor(n / 60)
  const s = (n % 60).toFixed(2).padStart(5, '0')
  return `${String(m).padStart(2, '0')}:${s}`
}

const formatDemoTime = (sec) => {
  const n = Math.max(0, Number(sec) || 0)
  const m = Math.floor(n / 60)
  const s = (n % 60).toFixed(2).padStart(5, '0')
  return `${String(m).padStart(2, '0')}:${s}`
}

const onSubtitleDemoLoadedMetadata = () => {
  const audio = subtitleDemoAudioRef.value
  subtitleDemoDuration.value = Number(audio?.duration) || 0
}

const startSubtitleClock = () => {
  const tick = () => {
    const audio = subtitleDemoAudioRef.value
    if (!audio) return
    subtitleDemoCurrentTime.value = Number(audio.currentTime) || 0
    if (!audio.paused && !audio.ended) {
      subtitleDemoRafId.value = requestAnimationFrame(tick)
    } else {
      subtitleDemoRafId.value = null
    }
  }
  if (subtitleDemoRafId.value == null) {
    subtitleDemoRafId.value = requestAnimationFrame(tick)
  }
}

const stopSubtitleClock = () => {
  if (subtitleDemoRafId.value != null) {
    cancelAnimationFrame(subtitleDemoRafId.value)
    subtitleDemoRafId.value = null
  }
}

const onSubtitleDemoTimeUpdate = () => {
  const audio = subtitleDemoAudioRef.value
  subtitleDemoCurrentTime.value = Number(audio?.currentTime) || 0
}

const onSubtitleDemoPlay = () => {
  subtitleDemoPlaying.value = true
  startSubtitleClock()
}

const onSubtitleDemoPause = () => {
  subtitleDemoPlaying.value = false
  stopSubtitleClock()
}

const onSubtitleDemoEnded = () => {
  subtitleDemoPlaying.value = false
  stopSubtitleClock()
}

const onSubtitleDemoSeek = (event) => {
  const audio = subtitleDemoAudioRef.value
  if (!audio) return
  const t = Number(event?.target?.value) || 0
  audio.currentTime = t
  subtitleDemoCurrentTime.value = t
}

const generateTtsPreview = async () => {
  if (!ttsPreviewText.value.trim()) return
  ttsGenerating.value = true
  ttsError.value = ''

  // 釋放舊的預覽 URL
  if (ttsPreviewUrl.value && ttsPreviewUrl.value.startsWith('blob:')) {
    URL.revokeObjectURL(ttsPreviewUrl.value)
    ttsPreviewUrl.value = ''
  }

  try {
    const formData = new FormData()
    formData.append('text', ttsPreviewText.value.trim())
    formData.append('voice', globalSettings.value.tts.voice)
    formData.append('speed', String(globalSettings.value.tts.speed))
    formData.append('reference_text', referenceText.value.trim())

    if (cloneAudioFile.value) {
      formData.append('reference_audio', cloneAudioFile.value)
    }

    const res = await fetch(
      getApiEndpoint('/api/video-abstract/tts-preview'),
      {
        method: 'POST',
        body: formData,
      }
    )

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}))
      throw new Error(errData?.detail || `TTS 生成失敗 (${res.status})`)
    }

    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    ttsPreviewUrl.value = url
    tempObjectUrls.value.push(url)
  } catch (err) {
    ttsError.value = err.message || 'TTS 生成失敗'
  } finally {
    ttsGenerating.value = false
  }
}

const fillReferenceTextWithLocalAsr = async () => {
  if (!cloneAudioFile.value) return
  asrFilling.value = true
  ttsError.value = ''
  try {
    const formData = new FormData()
    formData.append('reference_audio', cloneAudioFile.value)
    const res = await fetch(getApiEndpoint('/api/video-abstract/reference-asr'), {
      method: 'POST',
      body: formData,
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || `本地 ASR 代填失敗 (${res.status})`)
    referenceText.value = String(data?.text || '').trim()
    schedulePersistRunSettings()
  } catch (err) {
    ttsError.value = err.message || '本地 ASR 代填失敗'
  } finally {
    asrFilling.value = false
  }
}

const backToUpload = async () => {
  resetImageSession()
  openingProject.value = false
  resetEphemeralUrls()
  if (mergedPreviewVideoUrl.value && mergedPreviewVideoUrl.value.startsWith('blob:')) {
    try { URL.revokeObjectURL(mergedPreviewVideoUrl.value) } catch {}
  }
  mergedPreviewVideoUrl.value = ''
  mergedPreviewThumbnailUrl.value = ''
  isMergedPreviewSelected.value = false
  await resetProjectState({ resetPdf: true })
  stage.value = 'upload'
  activeTab.value = 'script'
  statusMessage.value = ''
  errorMessage.value = ''
  renderMessage.value = ''
}

// --- Keyboard Navigation ---
const handleKeyDown = (e) => {
  // 只在進入工作區後才能切換
  if (stage.value !== 'workspace') return

  // 如果使用者正在輸入文字，不應觸發切換
  const activeTag = document.activeElement ? document.activeElement.tagName : ''
  if (activeTag === 'TEXTAREA' || activeTag === 'INPUT') return

  if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
    e.preventDefault()
    if (selectedSlideIndex.value > 0) {
      selectedSlideIndex.value--
    }
  } else if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
    e.preventDefault()
    if (selectedSlideIndex.value < slides.value.length - 1) {
      selectedSlideIndex.value++
    }
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleKeyDown)
})

onBeforeUnmount(() => {
  imageComponentMounted = false
  resetImageSession()
  stopSubtitleClock()
  if (scriptSaveTimer) clearTimeout(scriptSaveTimer)
  window.removeEventListener('keydown', handleKeyDown)
  tempObjectUrls.value.forEach((url) => {
    try { URL.revokeObjectURL(url) } catch {}
  })
  Object.values(renderedPageVideos.value || {}).forEach((url) => {
    if (typeof url === 'string' && url.startsWith('blob:')) {
      try { URL.revokeObjectURL(url) } catch {}
    }
  })
  if (typeof mergedPreviewVideoUrl.value === 'string' && mergedPreviewVideoUrl.value.startsWith('blob:')) {
    try { URL.revokeObjectURL(mergedPreviewVideoUrl.value) } catch {}
  }
})
</script>

<style scoped>
/* ── Root ─────────────────────────────────────────────────── */
.lab-page {
  height: 100%;          /* 填滿 AppShell main-wrapper 給的高度 */
  min-height: 0;
  padding: 16px;
  background: #0f172a;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-sizing: border-box;
}

/* ── Upload Stage ─────────────────────────────────────────── */
.upload-stage {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  padding: 12px;
  overflow: auto;  /* 上傳卡片在極小視窗才允許捲動 */
  box-sizing: border-box;
}

.upload-card {
  width: 100%;
  max-width: 1120px;
  min-width: 0;
}

.upload-card,
.workflow-card {
  background: #1f2937;
  color: #e5e7eb;
  border-radius: 14px;
}

.title {
  color: #60a5fa;
  font-weight: 700;
}

.subtitle {
  color: #cbd5e1;
}

.upload-area {
  background: #1b2432;
  border: 2px dashed #60a5fa;
  border-radius: 12px;
  cursor: pointer;
  position: relative;   /* 讓 overlay 能絕對定位 */
  user-select: none;
  transition: border-color 0.2s, background 0.2s;
}

/* 全覆蓋透明 overlay：統一接管 drag 事件 */
.upload-drag-overlay {
  position: absolute;
  inset: 0;
  z-index: 1; /* 在內容之上 */
  cursor: pointer;
}

.upload-area * {
  pointer-events: none;
}

/* drag-overlay 本身需要 pointer events */
.upload-drag-overlay {
  pointer-events: all !important;
}

.upload-area.drag-over {
  border-color: #3b82f6;
  background: #1e3a5f;
}

.upload-icon {
  font-size: 2.4rem;
}

.upload-text {
  font-size: 1.05rem;
  font-weight: 600;
}

.upload-tip {
  color: #94a3b8;
}

.dark-input {
  background: #111827;
  color: #f3f4f6;
  border-color: #374151;
}

.dark-input::placeholder {
  color: #94a3b8;
  opacity: 1;
}

.dark-input:focus {
  background: #0b1220;
  color: #f8fafc;
  border-color: #3b82f6;
  box-shadow: 0 0 0 0.2rem rgba(59, 130, 246, 0.18);
}

.dark-input:focus::placeholder {
  color: #9fb0c9;
}

.offline-badge {
  background: rgba(250, 204, 21, 0.15);
  border: 1px solid rgba(250, 204, 21, 0.4);
  color: #fcd34d;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 0.85rem;
  text-align: center;
}

.user-script-box {
  height: 12rem;
  max-height: 12rem;
  overflow: auto;
  resize: none;
  caret-color: #f8fafc;
}

/* ── Workspace Stage ──────────────────────────────────────── */
.workspace-stage {
  flex: 1;         /* 吃掉 lab-page 扣除 padding 後的剩餘空間 */
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.workflow-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  width: 100%;
}

/* card-body 作為真正的 flex 容器，高度填滿 workflow-card */
.workflow-card .card-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 12px 16px;
  gap: 8px;
  position: relative;   /* establish stacking context for z-index of children */
  isolation: isolate;   /* ensure z-index works correctly within this scope */
}

/* ── Tabs Row ─────────────────────────────────────────────── */
.tabs-row {
  position: relative;
  z-index: 100;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: nowrap;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
  padding-bottom: 10px;
  padding-left: 56px; /* 右移避開左上角 Sidebar 展開按鈕 */
  overflow-x: auto; /* 允許橫向捲動 */
  min-height: 52px; /* 確保高度充足，免得被滾動條遮擋 */
  overscroll-behavior-x: contain;
  pointer-events: all; /* explicit: ensure clicks register */
}

.tab-btn {
  background: #111827;
  border: 1px solid #374151;
  color: #e5e7eb;
  font-weight: 600;
  flex: 0 0 auto;
  min-width: 90px;
  text-align: center;
  padding: 8px 12px;
  font-size: 14px;
  white-space: nowrap;
  border-radius: 6px;
  transition: all 0.2s;
  cursor: pointer;
}
.tab-btn:hover {
  background: #1f2937;
}
.tab-btn.active {
  background: #3b82f6;
  border-color: #60a5fa;
  color: #fff;
  box-shadow: 0 0 10px rgba(59, 130, 246, 0.5);
}
.preview-tab {
  margin-left: auto;
}


/* ── Workflow Main Grid ───────────────────────────────────── */
.workflow-main {
  flex: 1;
  min-height: 0;
  display: flex;
  /* 移除 gap 以實現無縫接合的 UI */
  overflow: hidden;
  min-width: 0;
  container-type: inline-size;
}

/* ── Content Panel (Right) ────────────────────────────────── */
.content-panel {
  background: #0f172a;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  flex: 1; /* 吃掉所有剩餘寬度！ */
}

/* ── Script View (上下分割) ───────────────────────────────── */
.script-view {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Panel View (字幕 / 語音 / 預覽 tabs) ─────────────────── */
.panel-view {
  flex: 1;
  min-height: 0;
  overflow: auto;   /* 設定/語音頁允許內部捲動 */
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.preview-output-layout {
  flex: 1;
  min-height: 0;
  display: flex;
  overflow: hidden;
  background: #0f172a;
}

.variant-drawer {
  position: relative;
  width: 210px;
  flex: 0 0 210px;
  min-width: 0;
  height: 100%;
  transition: width 0.18s ease, flex-basis 0.18s ease;
  z-index: 12;
}

.variant-drawer.collapsed {
  width: 0;
  flex-basis: 0;
}

.variant-drawer-clip {
  width: 100%;
  height: 100%;
  overflow: hidden;
}

.variant-drawer-body {
  width: 210px;
  height: 100%;
  transition: opacity 0.12s ease;
}

.variant-drawer.collapsed .variant-drawer-body {
  opacity: 0;
  pointer-events: none;
}

.variant-drawer-toggle {
  position: absolute;
  right: -20px;
  top: 50%;
  width: 20px;
  height: 56px;
  transform: translateY(-50%);
  display: grid;
  place-items: center;
  border: 1px solid #26364d;
  border-left: 0;
  border-radius: 0 6px 6px 0;
  color: #7dd3fc;
  background: linear-gradient(90deg, #0b1220, #172033);
  box-shadow: 4px 0 10px rgba(2, 6, 23, 0.24);
  line-height: 1;
  z-index: 3;
  padding: 0;
}

.variant-drawer :deep(.variant-panel) {
  width: 210px;
  flex-basis: 210px;
}

.variant-drawer.collapsed .variant-drawer-toggle {
  border-left: 1px solid #26364d;
  border-radius: 0 6px 6px 0;
}

.variant-drawer-chevron {
  width: 7px;
  height: 7px;
  border-right: 2px solid currentColor;
  border-bottom: 2px solid currentColor;
  transform: rotate(-45deg);
  transform-origin: center;
}

.variant-drawer-chevron.open {
  transform: rotate(135deg);
}

.variant-drawer-toggle:hover {
  color: #ffffff;
  border-color: #38bdf8;
  background: #1e3a5f;
}

.preview-render-zone {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #0f172a;
}

/* 中寬度時若仍維持「產出影片欄 + 預覽區」左右排列，
   產出影片欄會吃掉整列寬度，導致右側影片預覽被擠到不可見。 */
@container (max-width: 1180px) {
  .preview-output-layout {
    flex-direction: column;
    overflow: auto;
  }

  .variant-drawer {
    width: 100%;
    flex: 0 0 150px;
    height: 150px;
    transition: height 0.18s ease, flex-basis 0.18s ease;
  }

  .variant-drawer.collapsed {
    width: 100%;
    height: 0;
    flex-basis: 0;
  }

  .variant-drawer-body {
    width: 100%;
  }

  .variant-drawer :deep(.variant-panel) {
    width: 100%;
    flex-basis: auto;
  }

  .variant-drawer-toggle {
    right: 18px;
    top: 100%;
    width: 46px;
    height: 26px;
    transform: none;
    border-radius: 0 0 8px 8px;
  }

  .preview-render-zone {
    flex: 1 1 auto;
    min-height: 360px;
  }

  .preview-panel.final {
    min-height: 300px;
  }
}

/* ── Preview Panel ────────────────────────────────────────── */
.preview-panel {
  background: #1e293b; /* 像是 PPT 編輯區的灰色畫布 */
  height: var(--preview-height, 350px);
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  flex-shrink: 0;
  padding: 24px; /* 讓投影片四周有足夠的畫布留白空間 */
}

.preview-panel.final {
  flex: 1;
  min-height: 0;
}

.preview-panel img {
  max-width: 100%;
  max-height: 100%;
  width: auto;
  height: auto;
  object-fit: contain;
  display: block;
  background: #fff; /* 確保投影片本身是白底紙張感覺 */
  box-shadow: 0 8px 24px rgba(0,0,0,0.6); /* 強烈一點的陰影，更像一張紙放在畫布中央 */
}

.script-generation-actions {
  position: absolute;
  inset: 0;
  z-index: 12;
  pointer-events: none;
}

.script-generation-buttons {
  position: absolute;
  right: 14px;
  bottom: 12px;
  display: flex;
  gap: 5px;
  pointer-events: auto;
}

.script-gen-btn {
  min-height: 28px;
  padding: 4px 7px;
  border: 1px solid rgba(96, 165, 250, 0.55);
  border-radius: 6px;
  background: rgba(15, 23, 42, 0.86);
  color: #dbeafe;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0;
  cursor: pointer;
}

.script-gen-btn:hover:not(:disabled) {
  background: rgba(37, 99, 235, 0.92);
  color: #ffffff;
}

.script-gen-btn.secondary {
  border-color: rgba(148, 163, 184, 0.55);
  color: #e2e8f0;
}

.script-gen-btn.secondary:hover:not(:disabled) {
  background: rgba(71, 85, 105, 0.95);
}

.script-gen-btn:disabled {
  opacity: 0.55;
  cursor: wait;
}

.resizer-y {
  flex: 0 0 14px;
  height: 14px;
  position: relative;
  cursor: row-resize;
  background: #111827;
  border-top: 1px solid #334155;
  border-bottom: 1px solid #334155;
  z-index: 20;
  touch-action: none;
}

.resizer-y::before {
  content: "";
  position: absolute;
  left: 50%;
  top: 50%;
  width: min(220px, 38%);
  height: 4px;
  transform: translate(-50%, -50%);
  border-radius: 999px;
  background: #64748b;
  box-shadow: 0 0 0 1px rgba(15, 23, 42, 0.55);
}

.resizer-y:hover,
.resizer-y:active {
  background: #1e293b;
}

.resizer-y:hover::before,
.resizer-y:active::before {
  background: #60a5fa;
}

.preview-output-video {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #000;
  border-radius: 6px;
}

.merged-export-controls {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  min-height: 64px;
  padding: 12px 14px;
  border-top: 1px solid #334155;
  background: #0f172a;
}

.merged-export-note {
  margin-right: auto;
  color: #94a3b8;
  font-size: 13px;
}

.export-download-primary,
.export-download-secondary {
  min-width: 132px;
  border-radius: 7px;
  font-weight: 700;
}

.export-download-primary {
  color: #ffffff;
  border: 1px solid #3b82f6;
  background: linear-gradient(135deg, #2563eb, #1d4ed8);
  box-shadow: 0 5px 14px rgba(37, 99, 235, 0.2);
}

.export-download-primary:hover {
  color: #ffffff;
  border-color: #60a5fa;
  background: linear-gradient(135deg, #3b82f6, #2563eb);
}

.export-download-secondary {
  color: #bae6fd;
  border: 1px solid #0369a1;
  background: #0c2438;
}

.export-download-secondary:hover {
  color: #e0f2fe;
  border-color: #38bdf8;
  background: #0c4a6e;
}

/* ── Dummy Slide (字幕預覽) ───────────────────────────────── */
.dummy-slide {
  background: #111827;
  border: 1px solid #334155;
  border-radius: 10px;
  min-height: 200px;
  position: relative;
  display: grid;
  place-items: center;
  overflow: hidden;
  aspect-ratio: 16 / 9;
  width: 100%;
}

.placeholder-text,
.dummy-bg {
  color: #94a3b8;
}

/* ── Script Textarea ──────────────────────────────────────── */
.script-area {
  width: 100%;
  flex: 1; /* 自動填滿滑桿下方的所有剩餘空間 */
  min-height: 0;
  resize: none;
  background: #0f172a !important; /* 跟外層背景融為一體的備忘錄/逐字稿深色底 */
  color: #f8fafc;
  border: none; /* 移除外框，實現無縫佈局 */
  border-radius: 0;
  caret-color: #f8fafc;
  padding: 16px;
  font-size: 1rem;
  line-height: 1.6;
  box-sizing: border-box;
}

.script-area::placeholder {
  color: #64748b;
  opacity: 1;
}

.script-area:focus {
  background: #0b1220 !important;
  color: #f8fafc !important;
  box-shadow: inset 0 2px 8px rgba(0,0,0,0.2); /* 改用內陰影，避免破壞無邊框佈局 */
  outline: none;
}

/* ── Clone Box ────────────────────────────────────────────── */
.clone-box {
  background: #0b1220;
  border: 1px solid #334155;
  border-radius: 10px;
  padding: 12px;
  flex-shrink: 0;
}

/* ── Subtitle Overlays ────────────────────────────────── */
.overlay-subtitle {
  position: absolute;
  max-width: 90%;
  text-align: center;
  word-break: break-word;
  z-index: 20;
  transition: opacity 0.2s;
  pointer-events: none;
  user-select: none;
}

.auto-subtitle,
.static-subtitle {
  border-radius: 12px;
  pointer-events: none !important;
  cursor: default !important;
}

/* ── Subtitle / TTS Editor Layouts ─────────────────────────── */
/* 左右並排的 layout，左側是控制面板，右側是預覽 */
/* 字幕預覽分區主外框 */
.subtitle-preview-right {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 10px;
  gap: 8px;
  overflow: hidden;
  align-items: center;
  justify-content: center;
}

/* 預覽圖容器（固定 16:9 stage，字幕位置以投影片畫面為基準） */
.subtitle-canvas-frame {
  width: 100%;
  height: 100%;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border-radius: 8px;
  overflow: hidden;
}

.subtitle-canvas-wrapper {
  width: 100%;
  height: auto;
  max-width: 100%;
  max-height: 100%;
  aspect-ratio: 16 / 9;
  position: relative;
  background: #000;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}

.subtitle-fast-overlay {
  position: absolute;
  left: 0;
  right: 0;
  display: flex;
  justify-content: center;
  pointer-events: none;
  z-index: 4;
}

.subtitle-fast-box {
  display: inline-flex;
  align-items: center;
  font-weight: 700;
  line-height: 1.18;
  white-space: nowrap;
  padding: 0.1em 0.5em;
  border-radius: 2px;
  letter-spacing: 0;
}

.subtitle-ass-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 3;
}

.subtitle-canvas-img {
  width: 100%;
  height: 100%;
  object-fit: fill;
  pointer-events: none;
  display: block;
}

.subtitle-canvas-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #64748b;
}

/* 下方播放器工具格 */
.subtitle-player-bar {
  flex: 0 0 auto;
  min-width: 0;
  background: #0f172a;
  border: 1px solid #1e293b;
  border-radius: 8px;
  padding: 10px 14px;
}

.subtitle-editor-layout,
.tts-editor-layout {
  display: flex;
  height: 100%;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}

.subtitle-auto-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
}

.subtitle-auto-card {
  border: 1px solid rgba(96, 165, 250, 0.22);
  background: linear-gradient(180deg, rgba(15, 32, 62, 0.9), rgba(11, 24, 44, 0.96));
  border-radius: 12px;
  padding: 12px 13px;
}

.subtitle-auto-title {
  font-size: 12px;
  font-weight: 700;
  color: #93c5fd;
  margin-bottom: 4px;
}

.subtitle-auto-value {
  font-size: 14px;
  font-weight: 700;
  color: #f8fafc;
  margin-bottom: 4px;
}

.subtitle-auto-note {
  font-size: 12px;
  line-height: 1.45;
  color: #94a3b8;
}

.waveform-wrap {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

@container (max-width: 1040px) {
  .subtitle-editor-layout {
    flex-direction: column;
    overflow-y: auto;
  }

  .subtitle-preview-right {
    flex: 1 1 auto;
    min-height: min(420px, 56vh);
    width: 100%;
    padding: 10px;
  }

  .subtitle-editor-layout :deep(.subtitle-controls-panel) {
    flex: 0 0 auto !important;
    width: 100% !important;
    min-width: 0 !important;
    max-width: none !important;
    border-right: 0 !important;
    border-bottom: 1px solid #1e293b !important;
  }
}

@media (max-width: 900px) {
  .lab-page {
    padding: 8px;
    overflow: auto;
  }

  .workflow-card,
  .workspace-stage {
    min-height: 0;
    overflow: visible;
  }

  .workflow-card .card-body {
    padding: 8px;
    overflow: visible;
  }

  .workflow-main {
    flex-direction: column;
    overflow-y: auto;
  }


  .content-panel {
    flex: 1 1 auto;
    min-height: min(420px, 70vh);
    overflow: visible;
  }

  .script-view,
  .panel-view {
    min-height: 0;
    overflow: auto;
  }

  .preview-panel {
    padding: 12px;
  }

  .preview-panel.final {
    min-height: 280px;
  }

  .preview-output-layout {
    flex-direction: column;
  }

  .subtitle-preview-right {
    padding: 8px;
    min-height: 300px;
  }

  .subtitle-canvas-frame {
    max-height: 62vh;
  }
}

@media (max-width: 600px) {
  .tabs-row {
    padding-left: 48px; /* 稍微縮小左邊距，讓出更多空間給按鈕 */
    padding-bottom: 6px;
    gap: 4px;
  }
  .tab-btn {
    min-width: 80px;
    font-size: 13px;
    padding: 6px 8px;
  }

  .upload-stage {
    align-items: flex-start;
    padding: 6px;
  }

  .upload-card .card-body {
    padding: 18px !important;
  }

  .upload-area {
    min-height: 132px;
  }

}

/* X 形清除按鈕，疊加在音波圖右上角 */
.waveform-delete-btn {
  position: absolute;
  top: 6px;
  right: 48px; /* 避開右側下載按鈕的重疊 */
  width: 26px;
  height: 26px;
  border-radius: 6px;
  background: rgba(239,68,68,0.85);
  color: white;
  border: none;
  cursor: pointer;
  font-size: 13px;
  line-height: 26px;
  text-align: center;
  font-weight: bold;
  z-index: 20;
  transition: background 0.15s;
  padding: 0;
}
.waveform-delete-btn:hover { background: rgba(220,38,38,1); }

</style>
