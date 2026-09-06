import { computed, onBeforeUnmount, ref, watch } from 'vue'

export function useRenderQueue({
  slides,
  selectedSlide,
  selectedSlideIndex,
  renderedPageVideos,
  rendering,
  renderingAll,
  renderMessage,
  currentRunId,
  globalSettings,
  subtitleOutputMode,
  referenceText,
  cloneAudioFile,
  selectedVoiceKey,
  enableSubtitleHighlight,
  selectedVariantIds,
  refreshRunManifest,
  getMergedDownloadFilename,
  getApiEndpoint,
  emitter,
  splitMinChars,
  splitMaxChars,
  persistRunSettingsNow,
  onMergedPreviewReady,
}) {
  const renderStopRequested = ref(false)
  let currentRenderAbortController = null
  const singleRenderQueue = ref([])
  const activeSingleRenderPage = ref(null)
  const renderingPageStatus = ref({})
  const activeBatchJobId = ref('')
  let batchPollAbortController = null
  let batchPollGeneration = 0
  let disposed = false

  const detachBatchPolling = () => {
    batchPollGeneration += 1
    try { batchPollAbortController?.abort() } catch {}
    batchPollAbortController = null
  }
  watch(currentRunId, () => {
    detachBatchPolling()
    renderingAll.value = false
    activeBatchJobId.value = ''
  })
  onBeforeUnmount(() => {
    disposed = true
    detachBatchPolling()
  })

  const cancellableSinglePages = computed(() => {
    const pages = []
    if (Number.isInteger(activeSingleRenderPage.value)) pages.push(activeSingleRenderPage.value)
    for (const pageIdx of singleRenderQueue.value) {
      if (!pages.includes(pageIdx)) pages.push(pageIdx)
    }
    return pages.slice(0, 3)
  })

  const renderablePageIndexes = computed(() => slides.value
    .map((slide, idx) => ({ idx, script: String(slide?.scriptText || '').trim() }))
    .filter((item) => item.script)
    .map((item) => item.idx))

  const requireRun = () => {
    if (!String(currentRunId.value || '').trim()) throw new Error('缺少專案 run_id，請重新上傳 PDF 或開啟專案。')
    return currentRunId.value
  }

  const hasRun = () => {
    try { requireRun(); return true } catch (error) {
      renderMessage.value = error.message
      return false
    }
  }

  const ensureRenderNotStopped = () => {
    requireRun()
    if (renderStopRequested.value) throw new Error('渲染已由使用者終止')
  }

  const abortError = (message = '批次輪詢已分離') => Object.assign(new Error(message), { name: 'AbortError' })

  const sleepWithAbort = (ms, signal) => new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError())
      return
    }
    const timer = setTimeout(done, ms)
    const onAbort = () => {
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      reject(abortError())
    }
    function done() {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })

  const fetchWithTimeout = async (url, options = {}, timeoutMs = 15000) => {
    const timeoutController = new AbortController()
    const parentSignal = options.signal
    const onParentAbort = () => timeoutController.abort()
    if (parentSignal) {
      if (parentSignal.aborted) timeoutController.abort()
      else parentSignal.addEventListener('abort', onParentAbort, { once: true })
    }
    const timer = setTimeout(() => timeoutController.abort(), timeoutMs)
    try {
      return await fetch(url, { ...options, signal: timeoutController.signal })
    } finally {
      clearTimeout(timer)
      parentSignal?.removeEventListener('abort', onParentAbort)
    }
  }

  const resolveInterruptedBatchJob = async (runId, jobId) => {
    if (disposed || runId !== currentRunId.value) return { detached: true }
    const shouldResume = window.confirm('上次工作意外中斷，是否使用上次設定繼續未完成部分？選擇取消將放棄恢復。')
    if (disposed || runId !== currentRunId.value) return { detached: true }
    const action = shouldResume ? 'resume' : 'cancel'
    const actionRes = await fetchWithTimeout(getApiEndpoint(
      `/api/video-runs/${encodeURIComponent(runId)}/jobs/${encodeURIComponent(jobId)}/${action}`,
    ), { method: 'POST' })
    const actionData = await actionRes.json().catch(() => ({}))
    if (disposed || runId !== currentRunId.value) return { detached: true }
    if (!actionRes.ok) throw new Error(actionData?.detail || `批次任務${shouldResume ? '恢復' : '取消'}失敗 (${actionRes.status})`)
    return shouldResume ? { resumed: true } : { cancelled: true }
  }

  const requestStopAllRendering = () => {
    const ok = window.confirm('確定要終止目前渲染流程嗎？')
    if (!ok) return
    renderStopRequested.value = true
    singleRenderQueue.value = []
    try { currentRenderAbortController?.abort() } catch {}
    if (currentRunId.value && activeBatchJobId.value) {
      fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/jobs/${encodeURIComponent(activeBatchJobId.value)}/cancel`), {
        method: 'POST',
      }).catch(() => {})
    }
  }

  const requestStopPage = (pageIdx) => {
    const ok = window.confirm(`確定要終止第 ${pageIdx + 1} 頁渲染嗎？`)
    if (!ok) return
    if (activeSingleRenderPage.value === pageIdx && rendering.value) {
      renderStopRequested.value = true
      try { currentRenderAbortController?.abort() } catch {}
      return
    }
    singleRenderQueue.value = singleRenderQueue.value.filter((x) => x !== pageIdx)
  }

  const createPageAudio = async (text, slideIdx = selectedSlideIndex.value) => {
    ensureRenderNotStopped()
    const formData = new FormData()
    formData.append('text', String(text || '').trim())
    formData.append('voice', globalSettings.value.tts.voice)
    formData.append('speed', String(globalSettings.value.tts.speed))
    formData.append('reference_text', referenceText.value.trim())
    formData.append('selected_voice_key', String(selectedVoiceKey?.value || ''))
    if (cloneAudioFile.value) formData.append('reference_audio', cloneAudioFile.value)
    formData.append('response_mode', 'json')

    currentRenderAbortController = new AbortController()
    const endpoint = getApiEndpoint(`/api/video-runs/${encodeURIComponent(requireRun())}/pages/${slideIdx}/tts`)
    const res = await fetch(endpoint, {
      method: 'POST',
      body: formData,
      signal: currentRenderAbortController.signal,
    })
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}))
      throw new Error(errData?.detail || `TTS 生成失敗 (${res.status})`)
    }
    const data = await res.json().catch(() => ({}))
    const variantId = String(data?.variant_id || data?.tts_id || '')
    if (!variantId) throw new Error('TTS 已完成，但後端未回傳變體 ID')
    return {
      persistent: true,
      ttsId: String(data?.tts_id || variantId),
      variantId,
      audioUrl: String(data?.audio_url || ''),
    }
  }

  const alignSubtitleForAudio = async (audioFile, text, slideIdx = selectedSlideIndex.value) => {
    ensureRenderNotStopped()
    const formData = new FormData()
    formData.append('text', String(text || '').trim())
    formData.append('split_min_chars', String(splitMinChars))
    formData.append('split_max_chars', String(splitMaxChars))
    if (audioFile?.ttsId) formData.append('tts_id', audioFile.ttsId)
    if (audioFile?.variantId) formData.append('variant_id', audioFile.variantId)
    currentRenderAbortController = new AbortController()
    const endpoint = getApiEndpoint(`/api/video-runs/${encodeURIComponent(requireRun())}/pages/${slideIdx}/align`)
    const res = await fetch(endpoint, {
      method: 'POST',
      body: formData,
      signal: currentRenderAbortController.signal,
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || `字幕對齊失敗 (${res.status})`)
    return {
      segments: Array.isArray(data?.segments) ? data.segments : [],
      backend: String(data?.backend || ''),
      alignId: String(data?.align_id || res.headers.get('X-Align-Id') || ''),
      variantId: String(data?.variant_id || res.headers.get('X-Variant-Id') || audioFile?.variantId || ''),
      warning: String(data?.warning || ''),
    }
  }

  const renderAssVideoFromPrepared = async (slideIdx, audioFile, aligned, outputMode = 'burn') => {
    ensureRenderNotStopped()
    const slide = slides.value[slideIdx]
    if (!slide) throw new Error('找不到指定投影片')
    if (!slide.thumbnailUrl) throw new Error(`第 ${slideIdx + 1} 頁尚無縮圖，請稍後再試`)
    if (outputMode === 'burn' && !aligned.segments.length) throw new Error(`第 ${slideIdx + 1} 頁字幕對齊結果為空`)
    renderingPageStatus.value = { ...renderingPageStatus.value, [slideIdx]: 'running' }

    const formData = new FormData()
    formData.append('segments_json', JSON.stringify(aligned.segments))
    formData.append('subtitle_mode', outputMode)
    formData.append('subtitle_style', 'bg-dark')
    formData.append('enable_highlight', String(enableSubtitleHighlight.value))
    formData.append('font_size', String(Number(globalSettings.value.subtitle.fontSize ?? 52)))
    formData.append('text_color', String(globalSettings.value.subtitle.color || '#ffffff'))
    formData.append('active_word_color', String(globalSettings.value.subtitle.activeWordColor || '#facc15'))
    formData.append('enable_background', String(Boolean(globalSettings.value.subtitle.enableBackground)))
    formData.append('bg_color', String(globalSettings.value.subtitle.bgColor || '#000000'))
    formData.append('bg_opacity', String(Number(globalSettings.value.subtitle.bgOpacity || 55)))
    formData.append('margin_v', String(Number(globalSettings.value.subtitle.marginV ?? 90)))
    formData.append('enable_outline', String(Boolean(globalSettings.value.subtitle.enableOutline)))
    formData.append('outline_color', String(globalSettings.value.subtitle.outlineColor || '#000000'))
    formData.append('outline_width', String(Number(globalSettings.value.subtitle.outlineWidth ?? 2)))
    formData.append('tts_voice', String(globalSettings.value.tts.voice || ''))
    formData.append('tts_speed', String(globalSettings.value.tts.speed ?? 1))
    formData.append('selected_voice_key', String(selectedVoiceKey?.value || ''))
    formData.append('reference_text', String(referenceText.value || ''))
    formData.append('align_backend', aligned.backend || '')
    if (audioFile?.ttsId) formData.append('tts_id', audioFile.ttsId)
    if (aligned?.alignId) formData.append('align_id', aligned.alignId)
    if (aligned?.variantId || audioFile?.variantId) formData.append('variant_id', aligned?.variantId || audioFile?.variantId)
    if (!aligned?.variantId && !audioFile?.variantId) throw new Error('缺少已保存的語音變體 ID')
    formData.append('run_id', requireRun())
    formData.append('page_index', String(slideIdx))
    formData.append('variant_label', `web-page-${slideIdx + 1}`)

    renderMessage.value = `第 ${slideIdx + 1} 頁：ASS 影片渲染中...`
    currentRenderAbortController = new AbortController()
    const res = await fetch(getApiEndpoint('/api/video-abstract/render-subtitle-ass-video'), {
      method: 'POST',
      body: formData,
      signal: currentRenderAbortController.signal,
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err?.detail || `第 ${slideIdx + 1} 頁影片渲染失敗 (${res.status})`)
    }

    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const variantId = res.headers.get('X-Variant-Id') || ''
    const prevUrl = renderedPageVideos.value[slideIdx]
    if (prevUrl && prevUrl.startsWith('blob:')) {
      try { URL.revokeObjectURL(prevUrl) } catch {}
    }
    renderedPageVideos.value = { ...renderedPageVideos.value, [slideIdx]: url }
    if (variantId) {
      selectedVariantIds.value = { ...selectedVariantIds.value, [slideIdx]: variantId }
      await refreshRunManifest()
      emitter.emit('refresh-video-runs')
    }
    renderingPageStatus.value = { ...renderingPageStatus.value, [slideIdx]: '' }
  }

  const processSingleRenderQueue = async () => {
    if (renderingAll.value || rendering.value) return
    const nextPage = singleRenderQueue.value.shift()
    if (nextPage == null) return
    selectedSlideIndex.value = nextPage
    await startSingleRender(nextPage)
  }

  const startSingleRender = async (idx) => {
    rendering.value = true
    activeSingleRenderPage.value = idx
    renderingPageStatus.value = { ...renderingPageStatus.value, [idx]: 'running' }
    renderStopRequested.value = false
    renderMessage.value = `第 ${idx + 1} 頁：準備渲染...`
    try {
      const slide = slides.value[idx]
      const scriptText = String(slide?.scriptText || '').trim()
      if (!scriptText) throw new Error(`第 ${idx + 1} 頁講稿為空，無法渲染`)
      renderMessage.value = `第 ${idx + 1} 頁：TTS 生成中...`
      const audioFile = await createPageAudio(scriptText, idx)
      const outputMode = subtitleOutputMode?.value || 'burn'
      const aligned = outputMode === 'none'
        ? { segments: [], backend: '', alignId: '', variantId: String(audioFile?.variantId || '') }
        : await alignSubtitleForAudio(audioFile, scriptText, idx)
      if (outputMode !== 'none') renderMessage.value = `第 ${idx + 1} 頁：字幕對齊完成，準備輸出...`
      await renderAssVideoFromPrepared(idx, audioFile, aligned, outputMode)
      renderMessage.value = aligned.warning
        ? `第 ${idx + 1} 頁渲染完成，但對齊可信度偏低，建議試聽確認。`
        : `第 ${idx + 1} 頁渲染完成。`
    } catch (err) {
      renderMessage.value = err?.name === 'AbortError' ? `第 ${idx + 1} 頁已終止。` : (err.message || '渲染失敗')
      renderingPageStatus.value = { ...renderingPageStatus.value, [idx]: '' }
    } finally {
      currentRenderAbortController = null
      rendering.value = false
      activeSingleRenderPage.value = null
      if (!renderingAll.value) await processSingleRenderQueue()
    }
  }

  const renderCurrentPage = async () => {
    if (!hasRun()) return
    if (!selectedSlide.value) {
      renderMessage.value = '請先選擇頁面。'
      return
    }
    const idx = selectedSlideIndex.value
    if (rendering.value || renderingAll.value) {
      if (activeSingleRenderPage.value === idx || singleRenderQueue.value.includes(idx)) {
        renderMessage.value = `第 ${idx + 1} 頁已在渲染/排隊中。`
        return
      }
      if (singleRenderQueue.value.length >= 3) {
        renderMessage.value = '單頁排隊最多 3 個，請先終止部分排隊。'
        return
      }
      singleRenderQueue.value.push(idx)
      renderMessage.value = `第 ${idx + 1} 頁已加入排隊。`
      return
    }
    await startSingleRender(idx)
  }

  const regenerateTtsChunk = async ({ variantId, chunkIndex, text }) => {
    if (!hasRun()) return
    if (!currentRunId.value || !variantId || rendering.value || renderingAll.value) return
    const pageIdx = selectedSlideIndex.value
    const scriptText = String(slides.value[pageIdx]?.scriptText || '').trim()
    rendering.value = true
    renderStopRequested.value = false
    renderingPageStatus.value = { ...renderingPageStatus.value, [pageIdx]: 'running' }
    try {
      renderMessage.value = `第 ${pageIdx + 1} 頁：重生第 ${Number(chunkIndex) + 1} 段語音...`
      const formData = new FormData()
      formData.append('text', String(text || '').trim())
      const res = await fetch(getApiEndpoint(
        `/api/video-runs/${encodeURIComponent(currentRunId.value)}/pages/${pageIdx}/variants/${encodeURIComponent(variantId)}/chunks/${Number(chunkIndex)}/regenerate`,
      ), { method: 'POST', body: formData })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `局部語音重生失敗 (${res.status})`)
      const nextVariantId = String(data?.variant_id || '')
      if (!nextVariantId) throw new Error('局部語音重生後未取得變體 ID')
      const alignedScriptText = String(data?.page_text || scriptText).trim()
      if (alignedScriptText && slides.value[pageIdx]) {
        // A locally edited chunk changes the spoken source.  Keep the page
        // script in lockstep before forced alignment; the normal script watch
        // persists it to the run manifest shortly afterwards.
        slides.value[pageIdx].scriptText = alignedScriptText
      }
      const audio = {
        persistent: true,
        ttsId: String(data?.tts_id || nextVariantId),
        variantId: nextVariantId,
        audioUrl: String(data?.audio_url || ''),
      }
      const outputMode = subtitleOutputMode?.value || 'burn'
      const aligned = outputMode === 'none'
        ? { segments: [], backend: '', alignId: '', variantId: nextVariantId, warning: '' }
        : await alignSubtitleForAudio(audio, alignedScriptText, pageIdx)
      await renderAssVideoFromPrepared(pageIdx, audio, aligned, outputMode)
      await refreshRunManifest({ applySelected: true })
      renderMessage.value = aligned.warning
        ? `第 ${pageIdx + 1} 頁局部重生完成，但對齊可信度偏低，建議試聽。`
        : `第 ${pageIdx + 1} 頁第 ${Number(chunkIndex) + 1} 段已重生並重新渲染。`
    } catch (error) {
      renderMessage.value = error?.message || '局部語音重生失敗'
    } finally {
      renderingPageStatus.value = { ...renderingPageStatus.value, [pageIdx]: '' }
      rendering.value = false
      currentRenderAbortController = null
    }
  }

  const waitForBackendBatchJob = async (jobId, pagesToRender, runId = currentRunId.value) => {
    const pollGeneration = ++batchPollGeneration
    try { batchPollAbortController?.abort() } catch {}
    const pollController = new AbortController()
    batchPollAbortController = pollController
    const stageLabels = { queued: '排隊', tts: 'TTS', alignment: '字幕對齊', render: '影片渲染' }
    const isCurrent = () => pollGeneration === batchPollGeneration && runId === currentRunId.value && !pollController.signal.aborted
    let networkFailures = 0
    try {
      while (isCurrent()) {
        let job
        try {
          const res = await fetchWithTimeout(getApiEndpoint(`/api/video-runs/${encodeURIComponent(runId)}/jobs/${encodeURIComponent(jobId)}`), { signal: pollController.signal })
          job = await res.json().catch(() => ({}))
          if (!res.ok) throw new Error(job?.detail || `讀取批次任務失敗 (${res.status})`)
          networkFailures = 0
        } catch (error) {
          if (!isCurrent()) throw abortError()
          if (++networkFailures > 3) throw error
          renderMessage.value = `批次狀態暫時無法讀取，${5 * networkFailures} 秒後重試...`
          await sleepWithAbort(5000 * networkFailures, pollController.signal)
          continue
        }
        if (!isCurrent()) throw abortError()
        const pageStates = job?.pages || {}
        const nextStatus = { ...renderingPageStatus.value }
        for (const pageIdx of pagesToRender) {
          const status = pageStates[String(pageIdx)]?.status || ''
          nextStatus[pageIdx] = status === 'rendered' ? '' : (status ? 'running' : '')
        }
        renderingPageStatus.value = nextStatus
        const stage = String(job?.stage || 'queued')
        const progress = Number(job?.stage_total || 0)
          ? ` ${Number(job?.stage_index || 0)}/${Number(job.stage_total)}`
          : ''
        const currentPage = Number.isInteger(job?.current_page_index)
          ? `（第 ${Number(job.current_page_index) + 1} 頁）`
          : ''
        const queue = job?.queue || {}
        if (job?.status === 'queued' || queue?.queue_state === 'queued') {
          const ahead = Number(queue?.jobs_ahead || 0)
          const position = Number(queue?.queue_position || 0)
          const active = queue?.active || null
          const activeStage = active
            ? `${stageLabels[String(active.stage || '')] || active.stage || '處理中'}${Number(active.stage_total || 0) ? ` ${Number(active.stage_index || 0)}/${Number(active.stage_total)}` : ''}`
            : '準備切換任務'
          renderMessage.value = `正在等待其他任務：前方 ${ahead} 個${position ? `（等待序號 ${position}）` : ''}；目前工作站：${activeStage}。`
        } else {
          renderMessage.value = `${stageLabels[stage] || stage}${progress}${currentPage}：後端任務執行中，可重新整理後續跑。`
        }
        if (job?.status === 'interrupted') {
          const recovery = await resolveInterruptedBatchJob(runId, jobId)
          if (recovery.detached) throw abortError()
          if (recovery.cancelled) throw abortError('已放棄恢復上次中斷的批次渲染。')
          continue
        }
        if (job?.status === 'completed') return job
        if (job?.status === 'cancelled') throw Object.assign(new Error('批次渲染已終止。'), { name: 'AbortError' })
        if (job?.status === 'failed') throw new Error(job?.error || '後端批次渲染失敗')
        await sleepWithAbort(5000, pollController.signal)
      }
      throw abortError()
    } finally {
      if (batchPollAbortController === pollController) batchPollAbortController = null
    }
  }

  const renderAllPagesWithBackendJob = async (pagesToRender) => {
    const runId = currentRunId.value
    if (!runId) throw new Error('目前沒有可用的 run')
    const outputMode = subtitleOutputMode?.value || 'burn'
    if (typeof persistRunSettingsNow === 'function') {
      const saved = await persistRunSettingsNow({ includeReferenceAudio: Boolean(cloneAudioFile.value) })
      if (runId !== currentRunId.value) throw abortError()
      if (saved === false) throw new Error('語音設定尚未成功保存，無法開始批次渲染。')
    }
    const res = await fetchWithTimeout(getApiEndpoint(`/api/video-runs/${encodeURIComponent(runId)}/jobs/render`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page_indexes: pagesToRender,
        subtitle_mode: outputMode,
        split_min_chars: splitMinChars,
        split_max_chars: splitMaxChars,
        tts_voice: String(globalSettings.value.tts.voice || ''),
        tts_speed: Number(globalSettings.value.tts.speed || 1),
        selected_voice_key: String(selectedVoiceKey?.value || ''),
        reference_text: String(referenceText.value || ''),
        subtitle_settings: {
          enable_highlight: Boolean(enableSubtitleHighlight.value),
          font_size: Number(globalSettings.value.subtitle.fontSize ?? 52),
          enable_background: Boolean(globalSettings.value.subtitle.enableBackground),
          bg_color: String(globalSettings.value.subtitle.bgColor || '#000000'),
          bg_opacity: Number(globalSettings.value.subtitle.bgOpacity || 55),
          margin_v: Number(globalSettings.value.subtitle.marginV ?? 90),
        },
      }),
    })
    const job = await res.json().catch(() => ({}))
    if (runId !== currentRunId.value) throw abortError()
    if (!res.ok) throw new Error(job?.detail || `建立批次任務失敗 (${res.status})`)
    activeBatchJobId.value = String(job?.job_id || '')
    if (!activeBatchJobId.value) throw new Error('後端未回傳批次任務 ID')
    const completed = await waitForBackendBatchJob(activeBatchJobId.value, pagesToRender, runId)
    if (runId !== currentRunId.value) throw abortError()
    await refreshRunManifest({ applySelected: true })
    if (runId !== currentRunId.value) throw abortError()
    const warnings = Object.entries(completed?.pages || {})
      .filter(([, state]) => state?.warning)
      .map(([index]) => Number(index) + 1)
    renderMessage.value = warnings.length
      ? `全部渲染完成；第 ${warnings.join('、')} 頁對齊可信度偏低，建議試聽確認。`
      : `全部渲染完成（${pagesToRender.length} 頁）。`
  }

  const reattachActiveBatchJob = async () => {
    if (disposed || !currentRunId.value || renderingAll.value) return false
    const attachedRunId = currentRunId.value
    const res = await fetchWithTimeout(getApiEndpoint(
      `/api/video-runs/${encodeURIComponent(attachedRunId)}/jobs-current`,
    ))
    if (disposed || attachedRunId !== currentRunId.value) return false
    if (res.status === 204) return false
    const job = await res.json().catch(() => ({}))
    if (disposed || attachedRunId !== currentRunId.value) return false
    if (!res.ok) throw new Error(job?.detail || `讀取進行中任務失敗 (${res.status})`)
    const jobId = String(job?.job_id || '')
    if (!jobId) return false
    if (String(job?.status || '') === 'interrupted') {
      const recovery = await resolveInterruptedBatchJob(attachedRunId, jobId)
      if (recovery.detached) return false
      if (recovery.cancelled) {
        renderMessage.value = '已放棄恢復上次中斷的批次渲染。'
        return true
      }
    }
    const requested = Array.isArray(job?.payload?.page_indexes)
      ? job.payload.page_indexes.map(Number).filter(Number.isInteger)
      : renderablePageIndexes.value
    activeBatchJobId.value = jobId
    renderingAll.value = true
    renderStopRequested.value = false
    try {
      await waitForBackendBatchJob(jobId, requested, attachedRunId)
      if (!disposed && attachedRunId === currentRunId.value) {
        await refreshRunManifest({ applySelected: true })
        if (disposed || attachedRunId !== currentRunId.value) return false
        renderMessage.value = `已接續並完成批次渲染（${requested.length} 頁）。`
      }
    } catch (error) {
      if (attachedRunId === currentRunId.value && error?.name !== 'AbortError') {
        renderMessage.value = error?.message || '批次渲染失敗'
      }
    } finally {
      if (attachedRunId === currentRunId.value && activeBatchJobId.value === jobId) {
        activeBatchJobId.value = ''
        renderingAll.value = false
      }
    }
    return true
  }

  const renderAllPages = async () => {
    if (!hasRun()) return
    if (!slides.value.length) return
    const pagesToRender = renderablePageIndexes.value
    if (!pagesToRender.length) {
      renderMessage.value = '沒有可渲染頁面：所有講稿皆為空。'
      return
    }
    if (rendering.value) {
      const active = Number.isInteger(activeSingleRenderPage.value) ? activeSingleRenderPage.value : null
      const nextQueue = [...singleRenderQueue.value]
      for (const idx of pagesToRender) {
        if (idx === active || nextQueue.includes(idx)) continue
        nextQueue.push(idx)
      }
      singleRenderQueue.value = nextQueue
      renderMessage.value = `已加入渲染全部 queue（共 ${pagesToRender.length} 個有講稿頁面，空講稿頁已跳過）。`
      return
    }
    if (renderingAll.value) {
      renderMessage.value = '批次渲染已在進行中。'
      return
    }
    const renderRunId = currentRunId.value
    renderingAll.value = true
    renderStopRequested.value = false
    singleRenderQueue.value = []
    const skipped = slides.value.length - pagesToRender.length
    renderMessage.value = skipped ? `開始批次渲染：${pagesToRender.length} 頁，跳過 ${skipped} 頁空講稿。` : '開始批次渲染...'
    try {
      await renderAllPagesWithBackendJob(pagesToRender)
    } catch (err) {
      if (renderRunId === currentRunId.value) {
        renderMessage.value = err?.name === 'AbortError' ? '批次渲染已終止。' : (err.message || '全部渲染失敗')
        Object.keys(renderingPageStatus.value || {}).forEach((k) => {
          if (renderingPageStatus.value[k] === 'running') renderingPageStatus.value[k] = ''
        })
      }
    } finally {
      currentRenderAbortController = null
      if (renderRunId === currentRunId.value) {
        activeBatchJobId.value = ''
        renderingAll.value = false
        if (singleRenderQueue.value.length) await processSingleRenderQueue()
      }
    }
  }

  const mergeAndDownloadRenderedVideos = async (transitionsEnabled = false) => {
    if (!hasRun()) return
    const transitionLabel = transitionsEnabled ? '，並加入系統隨機轉場' : ''
    const ok = window.confirm(`將依目前頁序合併已渲染影片${transitionLabel}（未渲染頁會跳過），並直接下載。確定執行？`)
    if (!ok) return
    const pageIndexes = slides.value.map((_, i) => i).filter((i) => !!renderedPageVideos.value[i])
    if (!pageIndexes.length) {
      renderMessage.value = '沒有可合併的已渲染影片。'
      return
    }
    try {
      renderMessage.value = `合併匯出中（${pageIndexes.length} 段）...`
      const formData = new FormData()
      formData.append('transitions_enabled', transitionsEnabled ? 'true' : 'false')
      const mergeUrl = getApiEndpoint(`/api/video-runs/${encodeURIComponent(requireRun())}/exports/merge-selected`)
      formData.append('run_id', requireRun())
      formData.append('page_indexes_json', JSON.stringify(pageIndexes))
      formData.append('variant_ids_json', JSON.stringify(selectedVariantIds.value || {}))
      formData.append('response_mode', 'video')
      const res = await fetch(mergeUrl, {
        method: 'POST',
        body: formData,
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err?.detail || `合併失敗 (${res.status})`)
      }
      const blob = await res.blob()
      const exportVariantId = res.headers.get('X-Export-Variant-Id') || ''
      const previewUrl = URL.createObjectURL(blob)
      if (typeof onMergedPreviewReady === 'function') {
        onMergedPreviewReady(previewUrl, exportVariantId)
      }
      const a = document.createElement('a')
      a.href = previewUrl
      a.download = typeof getMergedDownloadFilename === 'function'
        ? getMergedDownloadFilename()
        : 'merged_rendered_preview.mp4'
      a.click()
      if (currentRunId.value && exportVariantId) await refreshRunManifest()
      renderMessage.value = '合併匯出完成。'
    } catch (e) {
      renderMessage.value = e.message || '合併匯出失敗'
    }
  }

  return {
    singleRenderQueue,
    activeSingleRenderPage,
    renderingPageStatus,
    activeBatchJobId,
    cancellableSinglePages,
    renderablePageIndexes,
    requestStopAllRendering,
    requestStopPage,
    renderCurrentPage,
    renderAllPages,
    reattachActiveBatchJob,
    regenerateTtsChunk,
    mergeAndDownloadRenderedVideos,
  }
}
