/**
 * hermes-imagegen — desktop half.
 *
 * A full-page workbench at `/imagegen` (plus a sidebar row and a ⌘K command):
 * compose a prompt, pick channel/model/size/clarity/count, optionally attach
 * reference images, and watch the generation run through the plugin's own
 * backend (`/api/plugins/hermes-imagegen/`).
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax. Only
 * @hermes/plugin-sdk, react and react/jsx-runtime resolve.
 *
 * Credential rule: the API key never reaches this file. Everything goes through
 * `ctx.rest`, and the backend answers with masked channels only.
 */

import {
  Badge,
  Button,
  EmptyState,
  GlyphSpinner,
  Input,
  PALETTE_AREA,
  SegmentedControl,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Separator,
  Switch,
  Textarea,
  Tip,
  atom,
  cn,
  haptic,
  host,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-imagegen'
const ROUTE = '/imagegen'
const STORAGE_DRAFT = 'draft'

const RATIOS = ['1:1', '4:3', '3:2', '16:9', '21:9', '3:4', '2:3', '9:16', 'auto']
const QUALITIES = [
  { id: 'auto', label: '自动' },
  { id: '1k', label: '1K' },
  { id: '2k', label: '2K' },
  { id: '4k', label: '4K' }
]
const COUNTS = [
  { id: '1', label: '1' },
  { id: '2', label: '2' },
  { id: '3', label: '3' },
  { id: '4', label: '4' }
]
const MODES = [
  { id: 'generate', label: '文生图' },
  { id: 'edit', label: '图生图' }
]

/** The plugin context, captured in register() — components reach it from here. */
let api = null

const $settings = atom(null)
const $view = atom('studio')
const $draft = atom({
  mode: 'generate',
  channelId: '',
  model: '',
  promptModel: '',
  size: '1:1',
  quality: '2k',
  n: 1,
  prompt: ''
})
const $references = atom([])
// PNG data URL of the edit mask: transparent = "change here", opaque = keep the
// original. Same convention the OpenAI edits API uses (and the engine validates
// that its pixel size matches the first reference image).
const $mask = atom('')
const $job = atom(null)
const $error = atom('')
const $busy = atom(false)
const $version = atom('')

// ── helpers ────────────────────────────────────────────────────────────────

function errorText(error) {
  const raw = (error && (error.detail ?? error.message)) ?? '未知错误'
  if (raw && typeof raw === 'object') return raw.message || JSON.stringify(raw)
  const text = String(raw)
  const start = text.indexOf('{')
  if (start >= 0) {
    try {
      const parsed = JSON.parse(text.slice(start))
      const detail = parsed.detail ?? parsed
      if (typeof detail === 'string') return detail
      if (detail && typeof detail === 'object' && detail.message) return detail.message
    } catch {
      /* not JSON — use the text as-is */
    }
  }
  return text
}

function patchDraft(patch) {
  $draft.set({ ...$draft.get(), ...patch })
  api.storage.set(STORAGE_DRAFT, $draft.get())
}

function activeChannel(settings, channelId) {
  if (!settings) return null
  const channels = settings.channels || []
  return channels.find(channel => channel.id === channelId) || channels[0] || null
}

function isRunning(job) {
  return job != null && (job.status === 'queued' || job.status === 'running')
}

function formatDuration(ms) {
  if (typeof ms !== 'number' || ms <= 0) return ''
  if (ms < 1000) return `${ms} 毫秒`
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} 分 ${seconds % 60} 秒`
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error(`读取 ${file.name} 失败`))
    reader.readAsDataURL(file)
  })
}

async function addReferenceFiles(files, existing, limit = 5) {
  const accepted = Array.from(files || []).filter(file => file && file.type.startsWith('image/'))
  const room = Math.max(0, limit - existing.length)
  const chosen = accepted.slice(0, room)
  const added = []
  for (const file of chosen) {
    try {
      const dataUrl = await readFileAsDataUrl(file)
      if (dataUrl) added.push({ id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, name: file.name || 'reference', dataUrl })
    } catch {
      /* a single unreadable file must not abort the rest */
    }
  }
  return added
}

async function refreshSettings() {
  const data = await api.rest('/settings')
  $settings.set(data)
  return data
}

async function loadBootstrap() {
  try {
    const data = await api.rest('/bootstrap')
    if (data.version) $version.set(data.version)
    $settings.set(data.settings)
    const running = (data.jobs || []).find(job => isRunning(job))
    if (running) $job.set(running)
    const channels = data.settings?.channels || []
    const draft = $draft.get()
    if (!activeChannel(data.settings, draft.channelId)) {
      const first = channels[0]
      if (first) {
        patchDraft({ channelId: first.id, model: first.default_model || (first.models || [])[0] || '' })
      }
    }
    if (channels.length === 0) $view.set('channels')
  } catch (error) {
    $error.set(`读取插件状态失败：${errorText(error)}`)
  }
}

async function startJob() {
  const draft = $draft.get()
  const references = $references.get()
  if (!draft.channelId) {
    $error.set('请先在「渠道」里添加一个生图渠道')
    $view.set('channels')
    return
  }
  if (!draft.prompt.trim()) {
    $error.set('提示词不能为空')
    return
  }
  if (draft.mode === 'edit' && references.length === 0) {
    $error.set('图生图需要至少一张参考图')
    return
  }
  const mask = $mask.get()
  $error.set('')
  $busy.set(true)
  try {
    const body = {
      channel_id: draft.channelId,
      model: draft.model,
      mode: draft.mode,
      prompt: draft.prompt,
      size: draft.size,
      quality: draft.quality,
      n: draft.n,
      references: draft.mode === 'edit' ? references.map(reference => reference.dataUrl) : [],
      mask: draft.mode === 'edit' ? mask : ''
    }
    const response = await api.rest('/jobs', { method: 'POST', body, timeoutMs: 30000 })
    $job.set(response.job)
  } catch (error) {
    $error.set(errorText(error))
  } finally {
    $busy.set(false)
  }
}

async function cancelJob() {
  const job = $job.get()
  if (!job) return
  try {
    const response = await api.rest(`/jobs/${job.id}/cancel`, { method: 'POST' })
    $job.set(response.job)
  } catch (error) {
    $error.set(errorText(error))
  }
}

function useJobPolling() {
  const job = useValue($job)
  const jobId = job && isRunning(job) ? job.id : ''
  useEffect(() => {
    if (!jobId) return undefined
    return api.setInterval(async () => {
      const current = $job.get()
      if (!current || current.id !== jobId || !isRunning(current)) return
      try {
        const response = await api.rest(`/jobs/${jobId}`)
        if (response.job) $job.set(response.job)
      } catch {
        /* transient read failure: keep the interval, the next tick retries */
      }
    }, 1200)
  }, [jobId])
}

// ── image rendering ────────────────────────────────────────────────────────

function ImageView({ image, className, onOpen }) {
  const query = useQuery({
    queryKey: ['hermes-imagegen', 'image', image.id],
    queryFn: () => api.rest(`/files/${image.id}/inline`),
    staleTime: Infinity,
    gcTime: 30 * 60 * 1000
  })
  if (query.isPending) {
    return jsx('div', { className: cn('animate-pulse rounded-[4px] bg-(--ui-bg-tertiary)', className) })
  }
  if (query.error) {
    return jsx('div', {
      className: cn('flex items-center justify-center rounded-[4px] bg-(--ui-bg-tertiary) p-2 text-xs text-(--ui-text-tertiary)', className),
      children: '图片读取失败'
    })
  }
  return jsx('img', {
    alt: image.id,
    className: cn('block h-auto w-full rounded-[4px] border border-(--ui-stroke-secondary)', className),
    src: query.data?.data_url || '',
    onClick: onOpen ? () => onOpen(query.data?.data_url || '') : undefined
  })
}

function ResultCard({ image, index, job, onReuse }) {
  const queryClient = useQueryClient()
  const [downloading, setDownloading] = useState(false)
  const download = useCallback(async () => {
    setDownloading(true)
    try {
      const data = queryClient.getQueryData(['hermes-imagegen', 'image', image.id]) || (await api.rest(`/files/${image.id}/inline`))
      const extension = (data.mime || 'image/png').split('/')[1] || 'png'
      const anchor = document.createElement('a')
      anchor.href = data.data_url
      anchor.download = `imagegen-${job?.id || 'image'}-${index + 1}.${extension}`
      anchor.click()
    } catch (error) {
      host.notify({ kind: 'error', message: `下载失败：${errorText(error)}` })
    } finally {
      setDownloading(false)
    }
  }, [image.id, index, job, queryClient])

  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsx(ImageView, { image, className: 'cursor-zoom-in', onOpen: url => url && window.open(url, '_blank') }),
      jsxs('div', {
        className: 'flex items-center gap-1 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: `#${index + 1}` }),
          typeof image.size_bytes === 'number' ? jsx('span', { children: `· ${Math.max(1, Math.round(image.size_bytes / 1024))} KB` }) : null,
          jsx('span', { className: 'flex-1' }),
          jsx(Button, {
            size: 'xs',
            variant: 'ghost',
            disabled: downloading,
            onClick: download,
            children: downloading ? '保存中…' : '下载'
          }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: onReuse, children: '复用' })
        ]
      })
    ]
  })
}

function ResultPanel() {
  const job = useValue($job)
  useJobPolling()

  if (!job) {
    return jsx(EmptyState, {
      className: 'h-full',
      description: '左侧填提示词，点「开始生成」。结果会按顺序出现在这里，下载或复用参数随时可用。',
      title: '还没有生成结果'
    })
  }

  const reuse = () => {
    patchDraft({ prompt: job.prompt, mode: job.mode, model: job.model, channelId: job.channel_id, ...(job.params || {}) })
    $view.set('studio')
    haptic('tap')
  }

  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2 text-xs text-(--ui-text-tertiary)',
        children: [
          jsx(Badge, { children: job.channel_name || job.channel_id }),
          jsx(Badge, { children: job.model }),
          job.status === 'queued' ? jsx('span', { children: '排队中…' }) : null,
          job.status === 'running'
            ? jsxs('span', { className: 'flex items-center gap-1', children: [jsx(GlyphSpinner, {}), '生成中…'] })
            : null,
          job.status === 'succeeded' ? jsx('span', { children: `完成 · ${formatDuration(job.duration_ms)}` }) : null,
          job.status === 'failed' ? jsx('span', { className: 'text-(--ui-danger)', children: `失败：${job.error || job.error_code}` }) : null,
          job.status === 'cancelled' ? jsx('span', { children: '已取消' }) : null,
          job.status === 'lost' ? jsx('span', { className: 'text-(--ui-danger)', children: '任务状态已失效（网关重启过）' }) : null,
          jsx('span', { className: 'flex-1' }),
          isRunning(job)
            ? jsx(Button, { size: 'xs', variant: 'outline', onClick: cancelJob, children: '取消' })
            : null,
          job.images && job.images.length > 0 ? jsx(Button, { size: 'xs', variant: 'outline', onClick: reuse, children: '复用这组参数' }) : null
        ]
      }),
      job.images && job.images.length > 0
        ? jsx('div', {
            className: 'grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3',
            children: job.images.map((image, index) =>
              jsx(ResultCard, { image, index, job, key: image.id, onReuse: reuse })
            )
          })
        : isRunning(job)
          ? jsxs('div', {
              className: 'flex flex-col items-center gap-3 py-16 text-sm text-(--ui-text-tertiary)',
              children: [jsx(GlyphSpinner, {}), jsx('span', { children: '正在生成，最长约 4 分钟' })]
            })
          : null
    ]
  })
}

// ── reference images ───────────────────────────────────────────────────────

function MaskPainter({ reference, initialMask, onClose, onDone }) {
  const canvasRef = useRef(null)
  const paintingRef = useRef(false)
  const [brush, setBrush] = useState(48)
  const [ready, setReady] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return undefined
    let alive = true
    const image = new Image()
    image.onload = () => {
      if (!alive) return
      canvas.width = image.naturalWidth
      canvas.height = image.naturalHeight
      const ctx = canvas.getContext('2d', { willReadFrequently: true })
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      // Opaque = keep the original, so a fresh mask edits nothing.
      ctx.fillStyle = '#000000'
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      if (initialMask) {
        const maskImage = new Image()
        maskImage.onload = () => {
          if (!alive) return
          ctx.clearRect(0, 0, canvas.width, canvas.height)
          ctx.drawImage(maskImage, 0, 0, canvas.width, canvas.height)
          setReady(true)
        }
        maskImage.onerror = () => setError('已有遮罩读取失败')
        maskImage.src = initialMask
        return
      }
      setReady(true)
    }
    image.onerror = () => setError('参考图读取失败')
    image.src = reference.dataUrl
    return () => {
      alive = false
    }
  }, [reference, initialMask])

  const paint = event => {
    const canvas = canvasRef.current
    if (!canvas || !ready) return
    const rect = canvas.getBoundingClientRect()
    const scaleX = canvas.width / rect.width
    const scaleY = canvas.height / rect.height
    const x = (event.clientX - rect.left) * scaleX
    const y = (event.clientY - rect.top) * scaleY
    const ctx = canvas.getContext('2d')
    ctx.globalCompositeOperation = 'destination-out'
    ctx.beginPath()
    ctx.arc(x, y, Math.max(2, brush * scaleX), 0, Math.PI * 2)
    ctx.fill()
    ctx.globalCompositeOperation = 'source-over'
  }

  const clearAll = () => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    ctx.globalCompositeOperation = 'source-over'
    ctx.fillStyle = '#000000'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
  }

  const finish = () => {
    const canvas = canvasRef.current
    if (!canvas) return
    onDone(canvas.toDataURL('image/png'))
  }

  return jsxs('div', {
    className: 'fixed inset-0 z-50 flex flex-col gap-2 bg-(--ui-bg-primary)/95 p-3 text-xs',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('span', { className: 'font-medium', children: '画遮罩：涂过的地方才会被改，其余保持原图' }),
          jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: `尺寸 ${canvasRef.current ? canvasRef.current.width + '×' + canvasRef.current.height : '读取中'} · 必须与第一张参考图一致` }),
          jsx('span', { className: 'flex-1' }),
          jsx('span', { children: '笔刷' }),
          jsx('input', {
            type: 'range',
            min: 8,
            max: 200,
            value: brush,
            onChange: event => setBrush(Number(event.target.value))
          }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: clearAll, children: '清除遮罩' }),
          jsx(Button, { size: 'xs', onClick: finish, children: '完成' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: onClose, children: '取消' })
        ]
      }),
      error ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-danger)', children: error }) : null,
      jsx('div', {
        className: 'relative min-h-0 flex-1 overflow-auto rounded border border-(--ui-stroke-secondary) p-2',
        children: jsxs('div', {
          className: 'relative mx-auto',
          children: [
            jsx('img', {
              alt: '参考图',
              className: 'block max-h-full w-auto max-w-full select-none',
              draggable: false,
              src: reference.dataUrl
            }),
            jsx('canvas', {
              ref: canvasRef,
              className: 'absolute inset-0 h-full w-full cursor-crosshair opacity-60',
              onPointerDown: event => {
                paintingRef.current = true
                event.currentTarget.setPointerCapture(event.pointerId)
                paint(event)
              },
              onPointerMove: event => {
                if (paintingRef.current) paint(event)
              },
              onPointerUp: () => {
                paintingRef.current = false
              },
              onPointerLeave: () => {
                paintingRef.current = false
              }
            })
          ]
        })
      })
    ]
  })
}

function ReferenceStrip() {
  const references = useValue($references)
  const mask = useValue($mask)
  const [painting, setPainting] = useState(false)
  const input = useRef(null)

  const add = async files => {
    const added = await addReferenceFiles(files, $references.get())
    if (added.length > 0) $references.set([...$references.get(), ...added])
  }

  const remove = id => $references.set($references.get().filter(reference => reference.id !== id))

  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { children: `参考图 ${references.length}/5` }),
              references.length > 0
                ? jsx('button', {
                    className: 'hover:text-foreground',
                    onClick: () => setPainting(true),
                    type: 'button',
                    children: mask ? '遮罩：已设置（点开可改）' : '画遮罩（只改涂过的地方）'
                  })
                : null,
              mask
                ? jsx('button', {
                    className: 'hover:text-(--ui-danger)',
                    onClick: () => $mask.set(''),
                    type: 'button',
                    children: '移除遮罩'
                  })
                : null
            ]
          }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => input.current && input.current.click(), children: '添加图片' })
        ]
      }),
      jsx('input', {
        accept: 'image/*',
        multiple: true,
        ref: input,
        style: { display: 'none' },
        type: 'file',
        onChange: event => {
          add(event.target.files)
          event.target.value = ''
        }
      }),
      jsxs('div', {
        className: 'flex flex-wrap gap-1.5',
        onDragOver: event => event.preventDefault(),
        onDrop: event => {
          event.preventDefault()
          add(event.dataTransfer.files)
        },
        children: [
          ...references.map(reference =>
            jsxs('div', {
              className: 'group relative h-16 w-16 overflow-hidden rounded-[4px] border border-(--ui-stroke-secondary)',
              children: [
                jsx('img', { alt: reference.name, className: 'h-full w-full object-cover', src: reference.dataUrl }),
                jsx('button', {
                  'aria-label': '移除参考图',
                  className: 'absolute top-0 right-0 hidden bg-black/60 px-1 text-[10px] text-white group-hover:block',
                  onClick: () => remove(reference.id),
                  type: 'button',
                  children: '×'
                })
              ]
            }, reference.id)
          ),
          references.length < 5
            ? jsx('button', {
                className: 'flex h-16 w-16 items-center justify-center rounded-[4px] border border-dashed border-(--ui-stroke-secondary) text-xs text-(--ui-text-tertiary) hover:text-foreground',
                onClick: () => input.current && input.current.click(),
                type: 'button',
                children: '+'
              })
            : null
        ]
      }),
      jsx('div', {
        className: 'text-[0.625rem] text-(--ui-text-quaternary)',
        children: '支持拖拽、粘贴（在提示词框里 Ctrl/⌘+V）与多选；单张 ≤ 10MB'
      }),
      painting && references[0]
        ? jsx(MaskPainter, {
            initialMask: mask,
            reference: references[0],
            onClose: () => setPainting(false),
            onDone: dataUrl => {
              $mask.set(dataUrl)
              setPainting(false)
              haptic('tap')
            }
          })
        : null
    ]
  })
}

// ── studio ─────────────────────────────────────────────────────────────────

function StudioView() {
  const settings = useValue($settings)
  const draft = useValue($draft)
  const job = useValue($job)
  const error = useValue($error)
  const references = useValue($references)
  const running = isRunning(job)
  const channels = (settings && settings.channels) || []
  const channel = activeChannel(settings, draft.channelId)
  const models = (channel && channel.models) || []

  const detectModels = async () => {
    if (!channel) return
    setDetecting(true)
    try {
      const result = await api.rest('/channels/detect', {
        method: 'POST',
        body: { channel_id: channel.id, save: true },
        timeoutMs: 60000
      })
      const found = (result.models || []).map(model => model.id)
      await refreshSettings()
      // Pick the first detected model so the studio is immediately usable.
      if (found.length && !found.includes($draft.get().model)) {
        patchDraft({ model: found[0] })
      }
      host.notify({ kind: 'success', message: found.length ? `检测到 ${found.length} 个模型` : '没有检测到生图模型' })
    } catch (detectError) {
      host.notify({ kind: 'error', message: `检测失败：${errorText(detectError)}` })
    } finally {
      setDetecting(false)
    }
  }

  const [promptModels, setPromptModels] = useState([])
  const [promptBusy, setPromptBusy] = useState('')
  const [promptError, setPromptError] = useState('')
  const [detecting, setDetecting] = useState(false)

  useEffect(() => {
    if (!draft.channelId) {
      setPromptModels([])
      return undefined
    }
    let alive = true
    api.rest(`/prompt/models?channel_id=${encodeURIComponent(draft.channelId)}`)
      .then(data => {
        if (alive) setPromptModels(data.models || [])
      })
      .catch(() => {
        if (alive) setPromptModels([])
      })
    return () => {
      alive = false
    }
  }, [draft.channelId])

  const promptAction = async kind => {
    if (!draft.channelId) {
      setPromptError('先选一个渠道')
      return
    }
    setPromptBusy(kind)
    setPromptError('')
    try {
      const body = { channel_id: draft.channelId, model: draft.promptModel || '', prompt: draft.prompt }
      if (kind === 'reverse') {
        body.references = references.map(reference => reference.dataUrl)
        body.output = 'zh'
        body.detail = 'standard'
      }
      const data = await api.rest(`/prompt/${kind}`, { method: 'POST', body, timeoutMs: 130000 })
      if (data.prompt) patchDraft({ prompt: data.prompt, promptModel: data.model || draft.promptModel })
      haptic('tap')
    } catch (actionError) {
      setPromptError(errorText(actionError))
    } finally {
      setPromptBusy('')
    }
  }

  return jsx('div', {
    className: 'ig-root panel',
    children: [
      jsxs('header', {
        className: 'panelHeader',
        children: [
          jsx('span', {
            className: 'panelHeading',
            children: jsx('h2', { className: 'panelTitle', children: '生图' })
          }),
          jsx('span', {
            className: 'panelHeaderActions',
            children: jsx('span', {
              className: 'connectionStatus',
              'data-connected': 'true',
              children: [jsx('span', { className: 'connectionDot' }), '就绪']
            })
          })
        ]
      }),
      jsx('div', {
        className: 'studio',
        children: [
          jsx('div', { className: 'generation', children: jsx(ResultPanel, {}) }),
          jsxs('div', {
        className: 'config',
        children: [jsxs('div', { className: 'configScroll', children: [
          jsx(SegmentedControl, {
            onChange: mode => patchDraft({ mode }),
            options: MODES,
            value: draft.mode
          }),
          jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '渠道 / 模型' }),
              jsx(Select, {
                value: channel ? channel.id : '',
                onValueChange: id => {
                  const next = channels.find(item => item.id === id)
                  patchDraft({ channelId: id, model: next ? next.default_model || (next.models || [])[0] || '' : '' })
                },
                children: [
                  jsx(SelectTrigger, { className: 'h-8 w-full text-xs', children: jsx(SelectValue, { placeholder: '选择渠道' }) }),
                  jsx(SelectContent, {
                    children: channels.map(item =>
                      jsx(SelectItem, { value: item.id, children: item.name || item.id }, item.id)
                    )
                  })
                ]
              }),
              models.length > 0
                ? jsx(Select, {
                    value: draft.model,
                    onValueChange: model => patchDraft({ model }),
                    children: [
                      jsx(SelectTrigger, { className: 'h-8 w-full text-xs', children: jsx(SelectValue, { placeholder: '选择模型' }) }),
                      jsx(SelectContent, {
                        children: models.map(model => jsx(SelectItem, { value: model, children: model }, model))
                      })
                    ]
                  })
                : jsxs('div', {
                    className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: [
                      jsx('span', { children: channel ? '这个渠道还没有模型：' : '先选一个渠道' }),
                      channel
                        ? jsx('button', {
                            className: 'text-(--ui-accent) hover:underline',
                            disabled: detecting,
                            onClick: detectModels,
                            type: 'button',
                            children: detecting ? '检测中…' : '点这里检测模型'
                          })
                        : null
                    ]
                  })
            ]
          }),
          jsx(Textarea, {
            className: 'min-h-[104px] text-xs',
            placeholder: draft.mode === 'edit' ? '描述要怎么改这张图…' : '描述你想要的画面…',
            value: draft.prompt,
            onChange: event => patchDraft({ prompt: event.target.value }),
            onPaste: event => {
              const files = Array.from(event.clipboardData?.files || [])
              if (files.length > 0 && draft.mode === 'edit') {
                event.preventDefault()
                addReferenceFiles(files, $references.get()).then(added => {
                  if (added.length > 0) $references.set([...$references.get(), ...added])
                })
              }
            }
          }),
          jsxs('div', {
            className: 'flex items-center justify-between text-[0.625rem] text-(--ui-text-quaternary)',
            children: [
              jsx('span', { children: `${draft.prompt.length} 字` }),
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx('button', {
                    className: 'hover:text-foreground',
                    onClick: () => $view.set('templates'),
                    type: 'button',
                    children: '从模板库挑'
                  }),
                  jsx('button', {
                    className: 'hover:text-foreground',
                    onClick: () => patchDraft({ prompt: '' }),
                    type: 'button',
                    children: '清空提示词'
                  })
                ]
              })
            ]
          }),
          draft.mode === 'edit' ? jsx(ReferenceStrip, {}) : null,
          jsxs('div', {
            className: 'flex flex-wrap items-center gap-1',
            children: [
              jsx(Select, {
                value: draft.promptModel || '__auto__',
                onValueChange: value => patchDraft({ promptModel: value === '__auto__' ? '' : value }),
                children: [
                  jsx(SelectTrigger, { className: 'h-7 w-[132px] text-[0.6875rem]', children: jsx(SelectValue, { placeholder: '提示词模型' }) }),
                  jsx(SelectContent, {
                    children: [
                      jsx(SelectItem, { value: '__auto__', children: '自动选文本模型' }),
                      ...promptModels.map(model => jsx(SelectItem, { value: model, children: model }, model))
                    ]
                  })
                ]
              }),
              jsx(Tip, {
                label: '用渠道里的文本模型把上面的提示词扩写得更具体（温度 0.7）',
                children: jsx(Button, {
                  size: 'xs',
                  variant: 'outline',
                  disabled: Boolean(promptBusy),
                  onClick: () => promptAction('enhance'),
                  children: promptBusy === 'enhance' ? '增强中…' : '增强'
                })
              }),
              jsx(Tip, {
                label: '保持用途与事实不变，润色措辞、补视觉细节（温度 0.35）',
                children: jsx(Button, {
                  size: 'xs',
                  variant: 'outline',
                  disabled: Boolean(promptBusy),
                  onClick: () => promptAction('polish'),
                  children: promptBusy === 'polish' ? '润色中…' : '润色'
                })
              }),
              jsx(Tip, {
                label: '读参考图反推一段能复现它的提示词（需要至少一张参考图）',
                children: jsx(Button, {
                  size: 'xs',
                  variant: 'outline',
                  disabled: Boolean(promptBusy) || references.length === 0,
                  onClick: () => promptAction('reverse'),
                  children: promptBusy === 'reverse' ? '反推中…' : '从参考图反推'
                })
              })
            ]
          }),
          promptError ? jsx('div', { className: 'text-[0.625rem] break-words text-(--ui-danger)', children: promptError }) : null,
          jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '比例' }),
              jsx(Select, {
                value: draft.size,
                onValueChange: size => patchDraft({ size }),
                children: [
                  jsx(SelectTrigger, { className: 'h-8 w-full text-xs', children: jsx(SelectValue, {}) }),
                  jsx(SelectContent, {
                    children: RATIOS.map(ratio =>
                      jsx(SelectItem, { value: ratio, children: ratio === 'auto' ? '自动（由上游决定）' : ratio }, ratio)
                    )
                  })
                ]
              }),
              jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '清晰度' }),
              jsx(SegmentedControl, {
                onChange: quality => patchDraft({ quality }),
                options: QUALITIES,
                value: draft.quality
              }),
              jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '数量' }),
              jsx(SegmentedControl, {
                onChange: value => patchDraft({ n: Number(value) }),
                options: COUNTS,
                sizing: 'content',
                value: String(draft.n)
              })
            ]
          }),
          jsx(Separator, {}),
          jsxs('div', {
            className: 'flex items-center gap-1.5',
            children: [
              jsx(Button, {
                className: 'flex-1',
                disabled: running,
                onClick: startJob,
                size: 'sm',
                children: running ? '生成中…' : '开始生成'
              }),
              running ? jsx(Button, { onClick: cancelJob, size: 'sm', variant: 'outline', children: '取消' }) : null
            ]
          }),
          error ? jsx('div', { className: 'text-[0.6875rem] break-words text-(--ui-danger)', children: error }) : null
        ] })]
      }),
        ]
      })
    ]
  })
}

// ── channels ───────────────────────────────────────────────────────────────

const EMPTY_FORM = {
  id: '',
  name: '',
  api_url: '',
  api_key: '',
  api_key_env: '',
  api_url_full: false,
  protocol: 'auto',
  default_model: '',
  clear_api_key: false
}

function ChannelsView() {
  const settings = useValue($settings)
  const [form, setForm] = useState(EMPTY_FORM)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [detected, setDetected] = useState([])
  const channels = (settings && settings.channels) || []

  const patch = value => setForm(current => ({ ...current, ...value }))

  const edit = channel => {
    // Seed from the saved list so an already-detected channel can still be picked
    // without re-running detection after a restart.
    setDetected((channel.models || []).map(id => ({ id, known: true })))
    setStatus('')
    setForm({
      id: channel.id,
      name: channel.name || '',
      api_url: channel.api_url || '',
      api_key: '',
      api_key_env: channel.api_key_env || '',
      api_url_full: Boolean(channel.api_url_full),
      protocol: channel.protocol || 'auto',
      default_model: channel.default_model || '',
      clear_api_key: false
    })
  }

  /** Save the picked model on the channel and mirror it to the global default. */
  const pickModel = async modelId => {
    setForm(current => ({ ...current, default_model: modelId }))
    if (!form.id) {
      setStatus(`已选 ${modelId}，保存渠道后生效`)
      return
    }
    setBusy(true)
    try {
      const body = { ...form, default_model: modelId }
      delete body.api_key
      await api.rest('/settings/channels', { method: 'POST', body })
      const response = await api.rest('/settings/defaults', {
        method: 'POST',
        body: { default_channel_id: form.id, default_model: modelId }
      })
      $settings.set(response.settings)
      patchDraft({ channelId: form.id, model: modelId })
      setStatus(`默认模型：${modelId}`)
      haptic('tap')
    } catch (error) {
      setStatus(`保存模型失败：${errorText(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    setBusy(true)
    setStatus('')
    try {
      const body = { ...form }
      if (!body.api_key) delete body.api_key
      const response = await api.rest('/settings/channels', { method: 'POST', body })
      await refreshSettings()
      setForm(current => ({ ...current, id: response.channel.id, api_key: '' }))
      setStatus('已保存')
      haptic('tap')
    } catch (error) {
      setStatus(`保存失败：${errorText(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const detect = async () => {
    setBusy(true)
    setStatus('正在探测模型…')
    try {
      const body = form.id ? { channel_id: form.id, save: true } : { api_url: form.api_url, api_key: form.api_key, api_key_env: form.api_key_env, api_url_full: form.api_url_full, protocol: form.protocol }
      const result = await api.rest('/channels/detect', { method: 'POST', body, timeoutMs: 60000 })
      setDetected(result.models || [])
      if (result.channel) await refreshSettings()
      setStatus(`探测到 ${(result.models || []).length} 个生图模型`)
    } catch (error) {
      setStatus(`探测失败：${errorText(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const remove = async id => {
    setBusy(true)
    try {
      await api.rest(`/settings/channels/${id}`, { method: 'DELETE' })
      await refreshSettings()
      if (form.id === id) setForm(EMPTY_FORM)
      setStatus('已删除')
    } catch (error) {
      setStatus(`删除失败：${errorText(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const makeDefault = async id => {
    try {
      const response = await api.rest('/settings/defaults', { method: 'POST', body: { default_channel_id: id } })
      $settings.set(response.settings)
      setStatus('已设为默认渠道')
    } catch (error) {
      setStatus(`设置失败：${errorText(error)}`)
    }
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0',
    children: [
      jsxs('div', {
        className: 'flex w-[320px] shrink-0 flex-col gap-2 overflow-y-auto border-r border-(--ui-stroke-secondary) p-3',
        children: [
          jsxs('div', {
            className: 'flex items-center justify-between',
            children: [
              jsx('div', { className: 'text-sm font-medium', children: '渠道' }),
              jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setForm(EMPTY_FORM), children: '新建渠道' })
            ]
          }),
          channels.length === 0
            ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: '还没有渠道。右侧填 API 地址与密钥，保存后点「检测模型」。' })
            : channels.map(channel =>
                jsxs('div', {
                  className: cn(
                    'flex flex-col gap-1 rounded-[5px] border p-2',
                    form.id === channel.id ? 'border-(--ui-accent)' : 'border-(--ui-stroke-secondary)'
                  ),
                  children: [
                    jsxs('div', {
                      className: 'flex items-center gap-1.5 text-xs',
                      children: [
                        jsx('span', { className: 'truncate font-medium', children: channel.name || channel.id }),
                        settings && settings.default_channel_id === channel.id ? jsx(Badge, { children: '默认' }) : null,
                        channel.has_api_key ? null : jsx(Badge, { children: '无密钥' })
                      ]
                    }),
                    jsx('div', { className: 'truncate text-[0.625rem] text-(--ui-text-quaternary)', children: channel.api_url }),
                    jsxs('div', {
                      className: 'flex items-center gap-1 text-[0.625rem] text-(--ui-text-tertiary)',
                      children: [
                        jsx('span', { children: channel.default_model ? channel.default_model : `${(channel.models || []).length} 个模型` }),
                        jsx('span', { className: 'flex-1' }),
                        jsx('button', { className: 'hover:text-foreground', onClick: () => edit(channel), type: 'button', children: '编辑' }),
                        jsx('button', { className: 'hover:text-foreground', onClick: () => makeDefault(channel.id), type: 'button', children: '设为默认' }),
                        jsx('button', { className: 'hover:text-foreground', onClick: () => remove(channel.id), type: 'button', children: '删除' })
                      ]
                    })
                  ]
                }, channel.id)
              ),
          jsx('div', { className: 'flex-1' }),
          jsx(Button, { size: 'xs', variant: 'outline', onClick: () => $view.set('studio'), children: '回工作室' })
        ]
      }),
      jsxs('div', {
        className: 'min-w-0 flex-1 overflow-y-auto p-4',
        children: [
          jsxs('div', {
            className: 'flex max-w-[560px] flex-col gap-2',
            children: [
              jsx('div', { className: 'text-sm font-medium', children: form.id ? '编辑渠道' : '新建渠道' }),
              jsxs('div', {
                className: 'flex flex-col gap-1',
                children: [
                  jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '名称' }),
                  jsx(Input, {
                    className: 'h-8 text-xs',
                    placeholder: '例如：newapi / 官方 / 中转',
                    value: form.name,
                    onChange: event => patch({ name: event.target.value })
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex flex-col gap-1',
                children: [
                  jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'API 地址（base，如 https://host/v1）' }),
                  jsx(Input, {
                    className: 'h-8 font-mono text-xs',
                    placeholder: 'https://api.example.com/v1',
                    value: form.api_url,
                    onChange: event => patch({ api_url: event.target.value })
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex flex-col gap-1',
                children: [
                  jsx('div', {
                    className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: form.id ? 'API 密钥（留空 = 保持不变）' : 'API 密钥'
                  }),
                  jsx(Input, {
                    className: 'h-8 text-xs',
                    placeholder: form.id ? '不修改就留空' : 'sk-…',
                    type: 'password',
                    value: form.api_key,
                    onChange: event => patch({ api_key: event.target.value })
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex flex-col gap-1',
                children: [
                  jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '或从环境变量取密钥（可选）' }),
                  jsx(Input, {
                    className: 'h-8 font-mono text-xs',
                    placeholder: 'MY_IMAGE_API_KEY',
                    value: form.api_key_env,
                    onChange: event => patch({ api_key_env: event.target.value })
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx(Switch, { checked: form.api_url_full, onCheckedChange: value => patch({ api_url_full: value }) }),
                  jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '地址已是完整接口地址（不自动拼 /images/…）' })
                ]
              }),
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '协议' }),
                  jsx(SegmentedControl, {
                    onChange: protocol => patch({ protocol }),
                    options: [
                      { id: 'auto', label: '自动' },
                      { id: 'images', label: 'Images API' },
                      { id: 'chat-completions', label: 'Chat' }
                    ],
                    value: form.protocol
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex items-center gap-1.5 pt-1',
                children: [
                  jsx(Button, { disabled: busy, onClick: save, size: 'sm', children: busy ? '处理中…' : '保存' }),
                  jsx(Button, { disabled: busy || !form.api_url, onClick: detect, size: 'sm', variant: 'outline', children: '检测模型' }),
                  form.id
                    ? jsx(Button, {
                        size: 'sm',
                        variant: 'ghost',
                        onClick: async () => {
                          await api.rest('/settings/channels', { method: 'POST', body: { id: form.id, name: form.name, api_url: form.api_url, clear_api_key: true } })
                          await refreshSettings()
                          setStatus('已清除密钥')
                        },
                        children: '清除密钥'
                      })
                    : null
                ]
              }),
              status ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: status }) : null
            ]
          }),
          detected.length > 0
            ? jsxs('div', {
                className: 'mt-4 flex max-w-[560px] flex-col gap-1',
                children: [
                  jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '探测到的生图模型（点一个设为默认）' }),
                  jsx('div', {
                    className: 'flex flex-wrap gap-1',
                    children: detected.map(model =>
                      jsx('button', {
                        className: cn(
                          'rounded-[4px] border px-1.5 py-0.5 text-[0.6875rem]',
                          form.default_model === model.id
                            ? 'border-(--ui-accent) bg-(--ui-bg-tertiary) text-foreground'
                            : 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary) hover:text-foreground'
                        ),
                        disabled: busy,
                        onClick: () => pickModel(model.id),
                        type: 'button',
                        children: `${form.default_model === model.id ? '✓ ' : ''}${model.id}${model.known ? '' : '（未知协议）'}`
                      }, model.id)
                    )
                  }),
                  form.default_model
                    ? jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: `当前默认模型：${form.default_model}` })
                    : jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: '还没有选默认模型：不选的话工作室会默认用列表第一个' })
                ]
              })
            : null
        ]
      })
    ]
  })
}

// ── history / gallery ──────────────────────────────────────────────────────

const HISTORY_PAGE = 24

function formatStamp(seconds) {
  if (typeof seconds !== 'number' || seconds <= 0) return ''
  const date = new Date(seconds * 1000)
  const pad = value => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** Injects the generated panel stylesheet once (upstream DSH panel CSS). */
let panelCssLoaded = false

function ensurePanelCss() {
  if (panelCssLoaded || typeof document === 'undefined') return
  if (document.getElementById('hermes-imagegen-panel-css')) {
    panelCssLoaded = true
    return
  }
  panelCssLoaded = true
  api
    .rest('/ui.css')
    .then(data => {
      if (!data || !data.css) return
      const style = document.createElement('style')
      style.id = 'hermes-imagegen-panel-css'
      style.textContent = data.css
      document.head.appendChild(style)
    })
    .catch(() => {
      // The panel still renders with the app's own utility classes; the
      // generated stylesheet only sharpens the shape. No silent fallback claim.
      panelCssLoaded = false
    })
}

const GALLERY_MODEL_BADGE = model => {
  const lowered = String(model || '').toLowerCase()
  if (lowered.startsWith('nanobanana')) return 'N'
  if (lowered.startsWith('seedream')) return 'S'
  if (lowered.startsWith('grok')) return 'G'
  if (lowered.startsWith('qwen')) return 'Q'
  if (lowered.startsWith('glm')) return 'Z'
  return 'D'
}

/** One gallery card — DOM mirrors the upstream `article.galleryCard` tree. */
function GalleryCard({ record, index, selected, selectMode, onSelect, onEditTags, onLoad, onToggleFavorite, onDelete, onTagClick }) {
  const image = (record.images || [])[0]
  const query = useQuery({
    queryKey: ['hermes-imagegen', 'file', image ? image.id : ''],
    queryFn: () => api.rest(`/files/${image.id}/inline`),
    enabled: Boolean(image),
    staleTime: Infinity,
    gcTime: 30 * 60 * 1000,
    retry: 1
  })
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const seconds = typeof record.duration_ms === 'number' && record.duration_ms > 0 ? Math.round(record.duration_ms / 1000) : 0

  return jsxs('article', {
    className: 'galleryCard',
    'data-selected': selected ? '' : undefined,
    children: [
      jsx('label', {
        className: 'gallerySelect',
        title: '选择',
        children: jsx('input', {
          checked: selected,
          onChange: () => onSelect(record.id),
          type: 'checkbox'
        })
      }),
      jsxs('button', {
        className: 'galleryImageButton',
        'data-selecting': selectMode ? '' : undefined,
        onClick: () => (selectMode ? onSelect(record.id) : onLoad(record)),
        title: selectMode ? '选择' : '打开',
        type: 'button',
        children: [
          image
            ? query.data
              ? jsx('img', { alt: record.prompt || record.id, className: 'galleryImage', decoding: 'async', loading: 'lazy', src: query.data.data_url })
              : jsx('div', { className: 'galleryImage', children: null })
            : null,
          seconds ? jsx('span', { className: 'generationDuration', children: `耗时 ${Math.floor(seconds / 60)}分${seconds % 60}秒` }) : null,
          jsx('span', { className: 'galleryBadge', children: record.mode === 'edit' ? '图生图' : '文生图' })
        ]
      }),
      jsxs('div', {
        className: 'galleryCardActions',
        children: [
          jsxs('button', {
            className: 'galleryCardAction',
            onClick: event => {
              event.stopPropagation()
              onLoad(record)
            },
            type: 'button',
            children: [
              jsx('svg', { fill: 'none', height: 13, stroke: 'currentColor', strokeLinecap: 'round', strokeLinejoin: 'round', strokeWidth: 1.5, viewBox: '0 0 16 16', width: 13, children: jsx('path', { d: 'M3 3h10v10H3z M5.5 8h5' }) }),
              '载入参数'
            ]
          }),
          jsxs('button', {
            className: 'galleryCardAction',
            onClick: event => {
              event.stopPropagation()
              onToggleFavorite(record)
            },
            type: 'button',
            children: [
              jsx('svg', { fill: 'none', height: 13, stroke: 'currentColor', strokeLinecap: 'round', strokeLinejoin: 'round', strokeWidth: 1.5, viewBox: '0 0 16 16', width: 13, children: jsx('path', { d: 'M8 13.5 3.2 9a2.9 2.9 0 0 1 4.1-4.1L8 5.6l.7-.7A2.9 2.9 0 0 1 12.8 9Z' }) }),
              record.favorite ? '已收藏' : '收藏'
            ]
          })
        ]
      }),
      jsxs('div', {
        className: 'galleryCardFooter',
        children: [
          jsx('span', { className: 'galleryAvatar', children: GALLERY_MODEL_BADGE(record.model) }),
          jsxs('span', {
            className: 'galleryCardInfo',
            children: [
              jsx('strong', { children: record.prompt || '无提示词' }),
              jsx('small', { children: `${record.model || '—'} · ${(record.params && record.params.size) || '自动'} · ${formatStamp(record.created_at)}` }),
              jsxs('span', {
                className: 'galleryTags',
                children: [
                  ...(record.tags || []).map(tag =>
                    jsx('button', { onClick: () => onTagClick(tag), type: 'button', children: tag }, tag)
                  ),
                  jsx('button', {
                    className: 'galleryTagEdit',
                    onClick: () => {
                      setDraft((record.tags || []).join(' '))
                      setEditing(true)
                    },
                    title: '编辑标签',
                    type: 'button',
                    children: '+标签'
                  })
                ]
              })
            ]
          }),
          jsx('button', {
            className: 'galleryRemove',
            onClick: () => onDelete(record),
            title: '删除',
            type: 'button',
            children: '×'
          })
        ]
      }),
      editing
        ? jsxs('form', {
            className: 'galleryTagEditor',
            onSubmit: event => {
              event.preventDefault()
              onEditTags(record.id, draft.split(/[,，\s]+/).filter(Boolean))
              setEditing(false)
            },
            children: [
              jsx('input', {
                autoFocus: true,
                'aria-label': '标签',
                onChange: event => setDraft(event.target.value),
                placeholder: '标签，空格或逗号分隔',
                value: draft
              }),
              jsx('button', { type: 'submit', children: '保存' }),
              jsx('button', {
                onClick: () => {
                  setEditing(false)
                  setDraft('')
                },
                type: 'button',
                children: '取消'
              })
            ]
          })
        : null
    ]
  })
}

function HistoryView() {
  const [favoriteOnly, setFavoriteOnly] = useState(false)
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [tags, setTags] = useState([])
  const [tagFilter, setTagFilter] = useState('')
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState([])
  const [busy, setBusy] = useState(false)

  const load = useCallback(
    async (offset = 0, favorites = favoriteOnly, tag = tagFilter) => {
      setLoading(true)
      setError('')
      try {
        const tagQuery = tag ? `&tag=${encodeURIComponent(tag)}` : ''
        const data = await api.rest(
          `/history?limit=${HISTORY_PAGE}&offset=${offset}&favorite=${favorites ? 'true' : 'false'}${tagQuery}`
        )
        setTotal(data.total || 0)
        setItems(current => (offset === 0 ? data.items || [] : [...current, ...(data.items || [])]))
      } catch (readError) {
        setError(errorText(readError))
      } finally {
        setLoading(false)
      }
    },
    [favoriteOnly, tagFilter]
  )

  const loadTags = useCallback(async () => {
    try {
      const data = await api.rest('/history/tags')
      setTags(data.tags || [])
    } catch {
      setTags([])
    }
  }, [])

  useEffect(() => {
    load(0, favoriteOnly, tagFilter)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [favoriteOnly, tagFilter])

  useEffect(() => {
    loadTags()
  }, [loadTags])

  useEffect(() => {
    ensurePanelCss()
  }, [])

  const toggleFavorite = async record => {
    try {
      const response = await api.rest(`/history/${record.id}/favorite`, { method: 'POST', body: { value: !record.favorite } })
      setItems(current =>
        favoriteOnly && record.favorite
          ? current.filter(item => item.id !== record.id)
          : current.map(item => (item.id === record.id ? { ...item, favorite: response.generation.favorite } : item))
      )
    } catch (writeError) {
      host.notify({ kind: 'error', message: `收藏失败：${errorText(writeError)}` })
    }
  }

  const editTags = async (generationId, nextTags) => {
    try {
      const response = await api.rest(`/history/${generationId}/tags`, { method: 'POST', body: { tags: nextTags } })
      setItems(current => current.map(item => (item.id === generationId ? { ...item, tags: response.generation.tags } : item)))
      loadTags()
    } catch (writeError) {
      host.notify({ kind: 'error', message: `标签保存失败：${errorText(writeError)}` })
    }
  }

  const toggleSelect = id =>
    setSelected(current => (current.includes(id) ? current.filter(entry => entry !== id) : [...current, id]))

  const remove = async record => {
    try {
      await api.rest(`/history/${record.id}`, { method: 'DELETE' })
      setItems(current => current.filter(item => item.id !== record.id))
      setTotal(current => Math.max(0, current - 1))
      loadTags()
    } catch (writeError) {
      host.notify({ kind: 'error', message: `删除失败：${errorText(writeError)}` })
    }
  }

  const runBatch = async action => {
    if (selected.length === 0) return
    setBusy(true)
    try {
      const result = await api.rest('/history/batch', { method: 'POST', body: { ids: selected, action } })
      if (action === 'delete') {
        setItems(current => current.filter(item => !result.done.includes(item.id)))
        setTotal(current => Math.max(0, current - result.done.length))
      } else {
        const starred = action === 'favorite'
        setItems(current => current.map(item => (result.done.includes(item.id) ? { ...item, favorite: starred } : item)))
        if (favoriteOnly && !starred) {
          setItems(current => current.filter(item => !result.done.includes(item.id)))
        }
      }
      setSelected([])
      if (result.missing && result.missing.length) {
        host.notify({ kind: 'error', message: `${result.missing.length} 条记录已不存在，已跳过` })
      }
      loadTags()
    } catch (writeError) {
      host.notify({ kind: 'error', message: `批量操作失败：${errorText(writeError)}` })
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'ig-root flex h-full min-h-0 flex-col gap-2 p-3',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(SegmentedControl, {
            onChange: value => {
              setFavoriteOnly(value === 'favorite')
              setSelected([])
            },
            options: [
              { id: 'all', label: '全部' },
              { id: 'favorite', label: '收藏' }
            ],
            sizing: 'content',
            value: favoriteOnly ? 'favorite' : 'all'
          }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `共 ${total} 条` }),
          jsx(SegmentedControl, {
            onChange: value => {
              setSelectMode(value === 'select')
              setSelected([])
            },
            options: [
              { id: 'browse', label: '浏览' },
              { id: 'select', label: '多选' }
            ],
            sizing: 'content',
            value: selectMode ? 'select' : 'browse'
          }),
          jsx('span', { className: 'flex-1' }),
          jsx(Button, { size: 'xs', variant: 'ghost', disabled: loading, onClick: () => load(0, favoriteOnly, tagFilter), children: '刷新' })
        ]
      }),
      tags.length > 0
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-1',
            children: [
              jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: '标签：' }),
              ...tags.map(tag =>
                jsx('button', {
                  className: cn(
                    'rounded-[4px] px-1.5 py-0.5 text-[0.625rem]',
                    tagFilter === tag ? 'bg-(--ui-accent) text-white' : 'bg-(--ui-bg-tertiary) text-(--ui-accent) hover:opacity-80'
                  ),
                  onClick: () => setTagFilter(current => (current === tag ? '' : tag)),
                  type: 'button',
                  children: tag
                }, tag)
              ),
              tagFilter
                ? jsx('button', { className: 'text-[0.625rem] text-(--ui-text-quaternary) hover:text-foreground', onClick: () => setTagFilter(''), type: 'button', children: '清除筛选' })
                : null
            ]
          })
        : null,
      selected.length > 0
        ? jsxs('div', {
            className: 'flex items-center gap-2 rounded-[5px] border border-(--ui-accent) px-2 py-1',
            children: [
              jsx('span', { className: 'text-[0.6875rem]', children: `已选 ${selected.length} 条` }),
              jsx(Button, {
                size: 'xs',
                variant: 'ghost',
                onClick: () => setSelected(selected.length === items.length ? [] : items.map(item => item.id)),
                children: selected.length === items.length ? '取消全选' : '全选本页'
              }),
              jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy, onClick: () => runBatch('favorite'), children: '收藏' }),
              jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy, onClick: () => runBatch('unfavorite'), children: '取消收藏' }),
              jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy, onClick: () => runBatch('delete'), children: '删除' }),
              jsx('span', { className: 'flex-1' }),
              jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setSelected([]), children: '清空选择' })
            ]
          })
        : null,
      error ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-danger)', children: error }) : null,
      total === 0 && !loading
        ? jsx(EmptyState, {
            className: 'flex-1',
            description: favoriteOnly
              ? '还没有收藏的记录。'
              : tagFilter
                ? `没有带「${tagFilter}」标签的记录。`
                : '生成过的图片会按时间出现在这里，可载入参数、打标签、收藏或批量整理。',
            title: favoriteOnly ? '没有收藏' : '图库还是空的'
          })
        : jsxs('div', {
            className: 'min-h-0 flex-1 overflow-y-auto pr-1',
            children: [
              jsx('div', {
                className: 'galleryMasonry',
                'data-view': 'masonry',
                children: items.map(record =>
                  jsx(GalleryCard, {
                    key: record.id,
                    onDelete: remove,
                    onEditTags: editTags,
                    onLoad: dumpRecordToStudio,
                    onSelect: toggleSelect,
                    onTagClick: tag => setTagFilter(current => (current === tag ? '' : tag)),
                    onToggleFavorite: toggleFavorite,
                    record,
                    selectMode,
                    selected: selected.includes(record.id)
                  })
                )
              }),
              items.length < total
                ? jsx('div', {
                    className: 'flex justify-center py-3',
                    children: jsx(Button, {
                      size: 'xs',
                      variant: 'outline',
                      disabled: loading,
                      onClick: () => load(items.length, favoriteOnly, tagFilter),
                      children: loading ? '加载中…' : '加载更多'
                    })
                  })
                : null
            ]
          })
    ]
  })
}

// ── templates ──────────────────────────────────────────────────────────────

function TemplateThumb({ source, caseId, className }) {
  const query = useQuery({
    queryKey: ['hermes-imagegen', 'template', source, caseId],
    queryFn: () => api.rest(`/templates/image/${source}/${caseId}/inline`),
    staleTime: Infinity,
    gcTime: 30 * 60 * 1000,
    retry: 1
  })
  if (query.isPending) {
    return jsx('div', { className: cn('animate-pulse rounded-[3px] bg-(--ui-bg-tertiary)', className) })
  }
  if (query.error) {
    return jsx('div', {
      className: cn('flex items-center justify-center rounded-[3px] bg-(--ui-bg-tertiary) text-[0.5625rem] text-(--ui-text-quaternary)', className),
      children: '无图'
    })
  }
  return jsx('img', {
    alt: caseId,
    className: cn('rounded-[3px] border border-(--ui-stroke-secondary) object-cover', className),
    src: query.data?.data_url || ''
  })
}

/** One case as a card: 4:3 thumbnail on top, prompt underneath (upstream shape). */
function TemplateCard({ source, item, favorited, onUse, onToggleFavorite }) {
  const [expanded, setExpanded] = useState(false)
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-[6px] border border-(--ui-stroke-secondary) p-1.5',
    children: [
      jsx(TemplateThumb, { className: 'aspect-[4/3] w-full', caseId: item.id, source }),
      jsx('button', {
        className: 'line-clamp-1 text-left text-[0.6875rem] font-medium hover:underline',
        onClick: () => setExpanded(value => !value),
        type: 'button',
        children: item.title || item.id
      }),
      jsx('div', {
        className: cn('text-[0.625rem] leading-snug text-(--ui-text-tertiary)', expanded ? 'whitespace-pre-wrap break-words' : 'line-clamp-2'),
        children: item.prompt || ''
      }),
      jsxs('div', {
        className: 'mt-auto flex items-center gap-1 text-[0.5625rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', { className: 'min-w-0 flex-1 truncate', children: item.categoryZh || item.category || item.sourceLabel || '' }),
          jsx('button', {
            className: cn('shrink-0 hover:text-foreground', favorited ? 'text-(--ui-accent)' : ''),
            onClick: () => onToggleFavorite(item),
            type: 'button',
            children: favorited ? '♥' : '♡'
          }),
          jsx(Button, { className: 'shrink-0', size: 'xs', variant: 'ghost', onClick: () => onUse(item), children: '用这条' })
        ]
      })
    ]
  })
}

function TemplatesView() {
  const [sources, setSources] = useState([])
  const [source, setSource] = useState('vibeui')
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('')
  const [categories, setCategories] = useState([])
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [favorites, setFavorites] = useState([])
  const [favoritesOnly, setFavoritesOnly] = useState(false)
  const [refreshable, setRefreshable] = useState(false)

  const favoriteKeys = useMemo(() => new Set(favorites.map(entry => `${entry.sourceId}:${entry.id}`)), [favorites])

  const loadFavorites = useCallback(async () => {
    try {
      const data = await api.rest('/templates/favorites')
      setFavorites(data.favorites || [])
    } catch {
      setFavorites([])
    }
  }, [])

  useEffect(() => {
    api.rest('/templates/sources')
      .then(data => setSources(data.sources || []))
      .catch(readError => setError(errorText(readError)))
    loadFavorites()
  }, [loadFavorites])

  const load = useCallback(
    async (nextSource, offset = 0, options = {}) => {
      setLoading(true)
      setError('')
      try {
        const params = new URLSearchParams({
          source: nextSource,
          q: options.query ?? query,
          category: options.category ?? category,
          offset: String(offset),
          limit: '30'
        })
        const data = await api.rest(`/templates/list?${params.toString()}`)
        setTotal(data.total || 0)
        setCategories(data.categories || [])
        setItems(current => (offset === 0 ? data.cases || [] : [...current, ...(data.cases || [])]))
      } catch (readError) {
        setError(errorText(readError))
      } finally {
        setLoading(false)
      }
    },
    [query, category]
  )

  useEffect(() => {
    load(source, 0)
    const meta = sources.find(entry => entry.id === source)
    setRefreshable(Boolean(meta && meta.refreshable))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, sources.length])

  const use = item => {
    patchDraft({ prompt: item.prompt, mode: $draft.get().mode })
    $view.set('studio')
    haptic('tap')
  }

  const toggleFavorite = async item => {
    const key = `${source}:${item.id}`
    try {
      const data = favoriteKeys.has(key)
        ? await api.rest(`/templates/favorites/${source}/${item.id}`, { method: 'DELETE' })
        : await api.rest('/templates/favorites', { method: 'POST', body: { source, id: item.id } })
      setFavorites(data.favorites || [])
    } catch (writeError) {
      host.notify({ kind: 'error', message: `收藏失败：${errorText(writeError)}` })
    }
  }

  const refresh = async () => {
    setLoading(true)
    try {
      const data = await api.rest('/templates/refresh', { method: 'POST', body: { source }, timeoutMs: 60000 })
      host.notify({ kind: 'info', message: `已刷新：${data.total} 条案例` })
      await load(source, 0)
    } catch (readError) {
      setError(errorText(readError))
    } finally {
      setLoading(false)
    }
  }

  const visible = favoritesOnly
    ? favorites
        .filter(entry => entry.sourceId === source)
        .map(entry => ({ id: entry.id, title: entry.title, prompt: entry.prompt, categoryZh: '收藏', sourceLabel: '' }))
    : items

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-2 p-3',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(SegmentedControl, {
            onChange: next => {
              setCategory('')
              setFavoritesOnly(false)
              setSource(next)
            },
            options: sources.map(entry => ({ id: entry.id, label: entry.label || entry.id })),
            sizing: 'content',
            value: source
          }),
          jsx('span', { className: 'flex-1' }),
          refreshable
            ? jsx(Button, { size: 'xs', variant: 'ghost', disabled: loading, onClick: refresh, children: '从源站刷新' })
            : null,
          jsx(Button, {
            size: 'xs',
            variant: favoritesOnly ? 'default' : 'ghost',
            onClick: () => setFavoritesOnly(value => !value),
            children: `收藏 ${favorites.filter(entry => entry.sourceId === source).length}`
          })
        ]
      }),
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Input, {
            className: 'h-8 w-[240px] text-xs',
            placeholder: '搜索标题 / 提示词 / 标签，回车',
            value: query,
            onChange: event => setQuery(event.target.value),
            onKeyDown: event => {
              if (event.key === 'Enter') load(source, 0, { query: event.target.value })
            }
          }),
          jsx(Select, {
            value: category || '__all__',
            onValueChange: value => {
              const next = value === '__all__' ? '' : value
              setCategory(next)
              setFavoritesOnly(false)
              load(source, 0, { category: next })
            },
            children: [
              jsx(SelectTrigger, { className: 'h-8 w-[180px] text-xs', children: jsx(SelectValue, { placeholder: '全部分类' }) }),
              jsx(SelectContent, {
                children: [
                  jsx(SelectItem, { value: '__all__', children: '全部分类' }),
                  ...categories.map(entry => jsx(SelectItem, { value: entry.id, children: entry.label }, entry.id))
                ]
              })
            ]
          }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `共 ${favoritesOnly ? visible.length : total} 条` }),
          loading ? jsx(GlyphSpinner, {}) : null
        ]
      }),
      error ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-danger)', children: error }) : null,
      visible.length === 0 && !loading
        ? jsx(EmptyState, {
            className: 'flex-1',
            description: favoritesOnly ? '还没有收藏的模板。点卡片右下角的 ♡ 收藏。' : '换个关键词或分类试试。',
            title: favoritesOnly ? '没有收藏' : '没有匹配的案例'
          })
        : jsx('div', {
            className: 'min-h-0 flex-1 overflow-y-auto',
            children: jsxs('div', {
              className: 'grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] items-stretch gap-2',
              children: [
                ...visible.map(item =>
                  jsx(TemplateCard, {
                    key: `${source}:${item.id}`,
                    source,
                    item,
                    favorited: favoriteKeys.has(`${source}:${item.id}`),
                    onUse: use,
                    onToggleFavorite: toggleFavorite
                  })
                ),
                !favoritesOnly && items.length < total
                  ? jsx('div', {
                      className: 'col-span-full flex justify-center py-2',
                      children: jsx(Button, {
                        size: 'xs',
                        variant: 'outline',
                        disabled: loading,
                        onClick: () => load(source, items.length),
                        children: loading ? '加载中…' : '加载更多'
                      })
                    })
                  : null
              ]
            })
          })
    ]
  })
}

// ── canvas ─────────────────────────────────────────────────────────────────

const CANVAS_NODE_DEFAULTS = {
  text: { width: 260, height: 140, title: '提示词' },
  config: { width: 260, height: 220, title: '生成配置' },
  image: { width: 260, height: 260, title: '图片' }
}

function CanvasNodeFrame({ node, scale, selected, onSelect, onDragStart, children }) {
  return jsxs('div', {
    className: cn(
      'absolute flex flex-col overflow-hidden rounded-[6px] border bg-(--ui-bg-secondary) shadow-sm',
      selected ? 'border-(--ui-accent)' : 'border-(--ui-stroke-secondary)'
    ),
    style: {
      left: node.x * scale,
      top: node.y * scale,
      width: node.width * scale,
      height: node.height * scale
    },
    onPointerDown: event => {
      onSelect(node.id)
      event.stopPropagation()
    },
    children: [
      jsx('div', {
        className: 'flex h-5 shrink-0 cursor-move items-center justify-between gap-1 border-b border-(--ui-stroke-secondary) px-1.5 text-[0.625rem] text-(--ui-text-tertiary)',
        onPointerDown: event => {
          event.stopPropagation()
          onDragStart(event, node)
        },
        children: [
          jsx('span', { className: 'truncate', children: node.title || node.type }),
          jsx('span', { children: node.type })
        ]
      }),
      jsx('div', { className: 'flex min-h-0 flex-1 flex-col gap-1 p-1.5', children })
    ]
  })
}

function CanvasImageView({ asset, scale }) {
  const query = useQuery({
    queryKey: ['hermes-imagegen', 'canvas-asset', asset && asset.id],
    queryFn: () => api.rest(`/canvas/assets/${asset.id}/inline`),
    enabled: Boolean(asset && asset.id),
    staleTime: Infinity
  })
  if (!asset || !asset.id) return jsx('div', { className: 'grid flex-1 place-items-center text-[0.625rem] text-(--ui-text-quaternary)', children: '空图片' })
  if (query.isPending) return jsx('div', { className: 'flex-1 animate-pulse rounded bg-(--ui-bg-tertiary)' })
  if (query.error) return jsx('div', { className: 'grid flex-1 place-items-center text-[0.625rem] text-(--ui-danger)', children: '素材读取失败' })
  return jsx('img', { alt: asset.name || '', className: 'min-h-0 w-full flex-1 rounded object-contain', src: query.data?.data_url || '' })
}

function posterLines(ctx, text, width) {
  const lines = []
  for (const paragraph of String(text).split('\n')) {
    let line = ''
    for (const glyph of Array.from(paragraph)) {
      if (line !== '' && ctx.measureText(line + glyph).width > width) {
        lines.push(line)
        line = ''
      }
      line += glyph
    }
    lines.push(line)
  }
  return lines
}

function paintPoster(ctx, doc, images) {
  ctx.clearRect(0, 0, doc.width, doc.height)
  const background = images.get(doc.background.id)
  if (background) ctx.drawImage(background, 0, 0, doc.width, doc.height)
  for (const layer of doc.layers) {
    ctx.save()
    if (layer.type === 'image') {
      const image = images.get(layer.asset.id)
      if (image) ctx.drawImage(image, layer.x, layer.y, layer.width, layer.height)
    } else {
      ctx.beginPath()
      ctx.rect(layer.x, layer.y, layer.width, Math.max(0, doc.height - layer.y))
      ctx.clip()
      ctx.font = `${layer.bold ? 700 : 400} ${layer.fontSize}px ${layer.fontFamily}`
      ctx.fillStyle = layer.color
      ctx.textBaseline = 'top'
      ctx.textAlign = layer.align
      const x = layer.x + (layer.align === 'center' ? layer.width / 2 : layer.align === 'right' ? layer.width : 0)
      posterLines(ctx, layer.text, layer.width).forEach((line, index) => {
        ctx.fillText(line, x, layer.y + index * layer.fontSize * 1.25)
      })
    }
    ctx.restore()
  }
}

function PosterEditor({ asset, initialRecipe, onClose, onSaved }) {
  const [doc, setDoc] = useState(initialRecipe)
  const [selected, setSelected] = useState((initialRecipe.layers[0] || {}).id || '')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const canvasRef = useRef(null)
  const dragRef = useRef(null)
  const imagesRef = useRef(new Map())

  const assetIds = useMemo(() => {
    if (!doc) return []
    const ids = new Set([doc.background.id])
    for (const layer of doc.layers) if (layer.type === 'image') ids.add(layer.asset.id)
    return [...ids]
  }, [doc])

  useEffect(() => {
    let alive = true
    Promise.all(
      assetIds.map(async assetId => {
        if (imagesRef.current.has(assetId)) return null
        const data = await api.rest(`/canvas/assets/${assetId}/inline`)
        const image = new Image()
        await new Promise((resolve, reject) => {
          image.onload = () => resolve()
          image.onerror = () => reject(new Error('图片加载失败'))
          image.src = data.data_url
        })
        return [assetId, image]
      })
    )
      .then(pairs => {
        if (!alive) return
        for (const pair of pairs) if (pair) imagesRef.current.set(pair[0], pair[1])
        const canvas = canvasRef.current
        if (!canvas || !doc) return
        canvas.width = doc.width
        canvas.height = doc.height
        paintPoster(canvas.getContext('2d'), doc, imagesRef.current)
      })
      .catch(loadError => setError(errorText(loadError)))
    return () => {
      alive = false
    }
  }, [assetIds])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !doc) return
    canvas.width = doc.width
    canvas.height = doc.height
    paintPoster(canvas.getContext('2d'), doc, imagesRef.current)
  }, [doc])

  useEffect(() => {
    const move = event => {
      if (!dragRef.current || !doc) return
      const rect = canvasRef.current.getBoundingClientRect()
      const scale = doc.width / rect.width
      const layer = doc.layers.find(entry => entry.id === dragRef.current.layerId)
      if (!layer) return
      const x = Math.round(dragRef.current.originX + (event.clientX - dragRef.current.startX) * scale)
      const y = Math.round(dragRef.current.originY + (event.clientY - dragRef.current.startY) * scale)
      setDoc(current => ({
        ...current,
        layers: current.layers.map(entry =>
          entry.id === layer.id ? { ...entry, x: Math.max(0, Math.min(current.width - 8, x)), y: Math.max(0, Math.min(current.height - 8, y)) } : entry
        )
      }))
    }
    const up = () => {
      dragRef.current = null
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
  }, [doc])

  if (!doc) {
    return jsxs('div', {
      className: 'flex h-full items-center justify-center gap-2 text-xs',
      children: [
        jsx(GlyphSpinner, {}),
        jsx('span', { children: '准备海报底图…' }),
        error ? jsx('span', { className: 'text-(--ui-danger)', children: error }) : null
      ]
    })
  }

  const current = doc.layers.find(layer => layer.id === selected)
  const patchLayer = patch => {
    setDoc(prev => ({ ...prev, layers: prev.layers.map(layer => (layer.id === selected ? { ...layer, ...patch } : layer)) }))
  }

  const addText = () => {
    const id = `t_${Math.random().toString(36).slice(2, 8)}`
    const margin = Math.max(16, Math.round(doc.width * 0.06))
    setDoc(prev => ({
      ...prev,
      layers: [
        ...prev.layers,
        {
          id,
          type: 'text',
          x: margin,
          y: Math.round(prev.height * 0.7),
          width: prev.width - margin * 2,
          text: '新文字',
          fontSize: Math.max(18, Math.round(prev.height * 0.045)),
          fontFamily: 'sans-serif',
          color: '#ffffff',
          bold: false,
          align: 'left'
        }
      ]
    }))
    setSelected(id)
  }

  const addLogo = async () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/*'
    input.onchange = async () => {
      const file = input.files && input.files[0]
      if (!file) return
      setBusy('logo')
      try {
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader()
          reader.onload = () => resolve(String(reader.result || ''))
          reader.onerror = () => reject(new Error('读取文件失败'))
          reader.readAsDataURL(file)
        })
        const uploaded = await api.rest('/canvas/assets', { method: 'POST', body: { data_url: dataUrl, name: file.name } })
        const image = new Image()
        await new Promise((resolve, reject) => {
          image.onload = () => resolve()
          image.onerror = () => reject(new Error('图片加载失败'))
          image.src = dataUrl
        })
        imagesRef.current.set(uploaded.asset.id, image)
        const targetWidth = Math.round(doc.width * 0.25)
        const ratio = image.height / Math.max(1, image.width)
        const id = `l_${Math.random().toString(36).slice(2, 8)}`
        setDoc(prev => ({
          ...prev,
          layers: [
            ...prev.layers,
            {
              id,
              type: 'image',
              x: Math.round(prev.width * 0.06),
              y: Math.round(prev.height * 0.86),
              width: targetWidth,
              height: Math.round(targetWidth * ratio),
              asset: uploaded.asset
            }
          ]
        }))
        setSelected(id)
      } catch (uploadError) {
        setError(errorText(uploadError))
      } finally {
        setBusy('')
      }
    }
    input.click()
  }

  const removeLayer = () => {
    if (!current) return
    setDoc(prev => ({ ...prev, layers: prev.layers.filter(layer => layer.id !== current.id) }))
    setSelected('')
  }

  const exportPng = () => {
    const canvas = canvasRef.current
    if (!canvas) return
    const link = document.createElement('a')
    link.href = canvas.toDataURL('image/png')
    link.download = `poster-${Date.now()}.png`
    link.click()
  }

  const saveToCanvas = async () => {
    const canvas = canvasRef.current
    if (!canvas) return
    setBusy('save')
    try {
      const dataUrl = canvas.toDataURL('image/png')
      const uploaded = await api.rest('/canvas/assets', { method: 'POST', body: { data_url: dataUrl, name: 'poster.png' } })
      onSaved({ asset: uploaded.asset, poster: doc })
      haptic('success')
    } catch (saveError) {
      setError(errorText(saveError))
    } finally {
      setBusy('')
    }
  }

  return jsxs('div', {
    className: 'absolute inset-0 z-10 flex flex-col bg-(--ui-bg-primary)/95 p-2',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1',
        children: [
          jsx('span', { className: 'text-xs font-medium', children: `海报编辑 · ${doc.width}×${doc.height}` }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: addText, children: '+ 文字' }),
          jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy === 'logo', onClick: addLogo, children: '+ Logo' }),
          jsx(Button, { size: 'xs', variant: 'ghost', disabled: Boolean(busy), onClick: exportPng, children: '导出 PNG' }),
          jsx(Button, { size: 'xs', disabled: Boolean(busy), onClick: saveToCanvas, children: busy === 'save' ? '保存中…' : '保存到画布' }),
          jsx('span', { className: 'flex-1' }),
          error ? jsx('span', { className: 'max-w-[240px] truncate text-[0.625rem] text-(--ui-danger)', children: error }) : null,
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: onClose, children: '关闭' })
        ]
      }),
      jsxs('div', {
        className: 'mt-2 flex min-h-0 flex-1 gap-2',
        children: [
          jsx('div', {
            className: 'min-h-0 flex-1 overflow-auto rounded border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) p-2',
            children: jsx('canvas', {
              ref: canvasRef,
              className: 'mx-auto block h-auto max-h-full w-auto max-w-full cursor-move',
              onPointerDown: event => {
                const rect = event.currentTarget.getBoundingClientRect()
                const scale = doc.width / rect.width
                const px = (event.clientX - rect.left) * scale
                const py = (event.clientY - rect.top) * scale
                const hit = [...doc.layers].reverse().find(layer => {
                  const height = layer.type === 'image' ? layer.height : layer.fontSize * 1.25 * 3
                  return px >= layer.x && px <= layer.x + layer.width && py >= layer.y && py <= layer.y + height
                })
                if (!hit) {
                  setSelected('')
                  return
                }
                setSelected(hit.id)
                dragRef.current = { layerId: hit.id, startX: event.clientX, startY: event.clientY, originX: hit.x, originY: hit.y }
              }
            })
          }),
          jsxs('div', {
            className: 'flex w-[240px] shrink-0 flex-col gap-1 overflow-y-auto text-[0.625rem]',
            children: [
              jsx('div', {
                className: 'flex flex-col gap-0.5',
                children: doc.layers.map(layer =>
                  jsxs('button', {
                    className: cn('truncate rounded px-1 py-0.5 text-left', selected === layer.id ? 'bg-(--ui-bg-tertiary)' : 'hover:bg-(--ui-bg-tertiary)'),
                    onClick: () => setSelected(layer.id),
                    type: 'button',
                    children: [layer.type === 'text' ? '文字' : 'Logo', '：', layer.type === 'text' ? layer.text.slice(0, 12) : layer.asset.name || layer.asset.id.slice(0, 6)]
                  }, layer.id)
                )
              }),
              current
                ? jsxs('div', {
                    className: 'flex flex-col gap-1 border-t border-(--ui-stroke-secondary) pt-1',
                    children: [
                      current.type === 'text'
                        ? jsx(Textarea, {
                            className: 'h-16 w-full resize-none text-[0.625rem]',
                            value: current.text,
                            onChange: event => patchLayer({ text: event.target.value })
                          })
                        : null,
                      current.type === 'text'
                        ? jsxs('div', {
                            className: 'flex items-center gap-1',
                            children: [
                              jsx('input', { type: 'number', className: 'h-5 w-14 rounded border border-(--ui-stroke-secondary) bg-transparent px-1', value: current.fontSize, onChange: event => patchLayer({ fontSize: Number(event.target.value) || 8 }) }),
                              jsx('input', { type: 'color', className: 'h-5 w-8', value: /^#[0-9a-f]{6}$/i.test(current.color) ? current.color : '#ffffff', onChange: event => patchLayer({ color: event.target.value }) }),
                              jsx('select', { className: 'h-5 rounded border border-(--ui-stroke-secondary) bg-transparent', value: current.fontFamily, onChange: event => patchLayer({ fontFamily: event.target.value }), children: ['sans-serif', 'serif', 'monospace'].map(family => jsx('option', { value: family, children: family }, family)) }),
                              jsx('select', { className: 'h-5 rounded border border-(--ui-stroke-secondary) bg-transparent', value: current.align, onChange: event => patchLayer({ align: event.target.value }), children: ['left', 'center', 'right'].map(align => jsx('option', { value: align, children: align }, align)) }),
                              jsx('label', { className: 'flex items-center gap-0.5', children: [jsx('input', { type: 'checkbox', checked: current.bold, onChange: event => patchLayer({ bold: event.target.checked }) }), '粗'] })
                            ]
                          })
                        : null,
                      jsxs('div', {
                        className: 'flex items-center gap-1',
                        children: [
                          jsx('span', { children: '宽' }),
                          jsx('input', { type: 'number', className: 'h-5 w-14 rounded border border-(--ui-stroke-secondary) bg-transparent px-1', value: Math.round(current.width), onChange: event => patchLayer({ width: Number(event.target.value) || 8 }) }),
                          current.type === 'image'
                            ? jsxs('span', {
                                className: 'flex items-center gap-1',
                                children: [
                                  '高',
                                  jsx('input', { type: 'number', className: 'h-5 w-14 rounded border border-(--ui-stroke-secondary) bg-transparent px-1', value: Math.round(current.height), onChange: event => patchLayer({ height: Number(event.target.value) || 8 }) })
                                ]
                              })
                            : null
                        ]
                      }),
                      jsxs('div', {
                        className: 'flex items-center gap-1',
                        children: [
                          jsx('span', { children: 'x' }),
                          jsx('input', { type: 'number', className: 'h-5 w-14 rounded border border-(--ui-stroke-secondary) bg-transparent px-1', value: Math.round(current.x), onChange: event => patchLayer({ x: Number(event.target.value) || 0 }) }),
                          jsx('span', { children: 'y' }),
                          jsx('input', { type: 'number', className: 'h-5 w-14 rounded border border-(--ui-stroke-secondary) bg-transparent px-1', value: Math.round(current.y), onChange: event => patchLayer({ y: Number(event.target.value) || 0 }) })
                        ]
                      }),
                      jsx(Button, { size: 'xs', variant: 'ghost', onClick: removeLayer, children: '删除这一层' })
                    ]
                  })
                : jsx('div', { className: 'text-(--ui-text-quaternary)', children: '点画布上的元素或上面的列表来编辑' })
            ]
          })
        ]
      })
    ]
  })
}

function CanvasView() {
  const settings = useValue($settings)
  const [pages, setPages] = useState([])
  const [page, setPage] = useState(null)
  const [selected, setSelected] = useState('')
  const [busy, setBusy] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [posterNode, setPosterNode] = useState('')
  const [posterRecipe, setPosterRecipe] = useState(null)
  const dirtyRef = useRef(false)
  const revisionRef = useRef(0)
  const dragRef = useRef(null)
  const panRef = useRef(null)
  const viewportRef = useRef(null)

  const loadPages = useCallback(async () => {
    try {
      const data = await api.rest('/canvas/pages')
      setPages(data.pages || [])
    } catch (readError) {
      setError(errorText(readError))
    }
  }, [])

  useEffect(() => {
    loadPages()
  }, [loadPages])

  const openPage = useCallback(async pageId => {
    try {
      const data = await api.rest(`/canvas/pages/${pageId}`)
      setPage(data.page)
      revisionRef.current = data.page.revision
      setSelected('')
      setError('')
    } catch (readError) {
      setError(errorText(readError))
    }
  }, [])

  const save = useCallback(async silent => {
    if (!page) return null
    try {
      const data = await api.rest(`/canvas/pages/${page.id}`, {
        method: 'PUT',
        body: { document: page, revision: revisionRef.current }
      })
      revisionRef.current = data.page.revision
      setPage(current => (current ? { ...current, revision: data.page.revision } : current))
      dirtyRef.current = false
      if (!silent) setNote(`已保存 r${data.page.revision}`)
      return data.page
    } catch (saveError) {
      setError(errorText(saveError))
      return null
    }
  }, [page])

  useEffect(() => {
    if (!page || !dirtyRef.current) return undefined
    const timer = setTimeout(() => {
      save(true)
    }, 900)
    return () => clearTimeout(timer)
  }, [page, save])

  const update = mutator => {
    setPage(current => {
      if (!current) return current
      const next = mutator({ ...current, nodes: [...current.nodes], connections: [...current.connections] })
      dirtyRef.current = true
      return next
    })
  }

  const openPoster = async node => {
    const asset = node.metadata?.asset
    if (!asset || !asset.id) {
      setError('这个图片节点还没有素材，先上传或生成一张')
      return
    }
    setError('')
    if (node.metadata?.poster) {
      setPosterRecipe(node.metadata.poster)
      setPosterNode(node.id)
      return
    }
    try {
      const data = await api.rest(`/canvas/assets/${asset.id}/poster`)
      setPosterRecipe(data.recipe)
      setPosterNode(node.id)
    } catch (readError) {
      setError(errorText(readError))
    }
  }

  const onPosterSaved = ({ asset, poster }) => {
    const source = page.nodes.find(node => node.id === posterNode)
    const nodeId = `n_${Math.random().toString(36).slice(2, 10)}`
    update(current => {
      current.nodes.push({
        id: nodeId,
        type: 'image',
        title: '海报',
        x: (source ? source.x : 0) + 300,
        y: (source ? source.y : 0) + 300,
        width: 260,
        height: 260,
        metadata: { asset, poster, status: 'success' }
      })
      if (source) current.connections.push({ id: `e_${source.id}_${nodeId}`, fromNodeId: source.id, toNodeId: nodeId })
      return current
    })
    setPosterNode('')
    setPosterRecipe(null)
    setNote('海报已保存为新节点')
  }

  const createPage = async () => {
    setBusy('create')
    try {
      const data = await api.rest('/canvas/pages', { method: 'POST', body: { title: '未命名画布' } })
      await loadPages()
      setPage(data.page)
      revisionRef.current = data.page.revision
    } catch (writeError) {
      setError(errorText(writeError))
    } finally {
      setBusy('')
    }
  }

  const removePage = async pageId => {
    try {
      await api.rest(`/canvas/pages/${pageId}`, { method: 'DELETE' })
      if (page && page.id === pageId) setPage(null)
      await loadPages()
    } catch (writeError) {
      setError(errorText(writeError))
    }
  }

  const addNode = type => {
    const defaults = CANVAS_NODE_DEFAULTS[type]
    const id = `n_${Math.random().toString(36).slice(2, 10)}`
    const meta =
      type === 'text'
        ? { text: '' }
        : type === 'config'
          ? { mode: 'image', channelId: '', model: '', size: '1:1', quality: '2k', count: 1 }
          : { asset: { id: '', mime: 'image/png', name: '' }, status: 'idle' }
    const maxX = (page?.nodes || []).reduce((value, node) => Math.max(value, node.x + node.width), 0)
    update(current => {
      current.nodes.push({
        id,
        type,
        title: defaults.title,
        x: maxX + 40,
        y: 40 + Math.floor(Math.random() * 60),
        width: defaults.width,
        height: defaults.height,
        metadata: meta
      })
      return current
    })
    setSelected(id)
  }

  const patchNode = (nodeId, metaPatch) => {
    update(current => {
      const node = current.nodes.find(entry => entry.id === nodeId)
      if (node) node.metadata = { ...(node.metadata || {}), ...metaPatch }
      return current
    })
  }

  const toggleInput = (configId, sourceId) => {
    update(current => {
      const existing = current.connections.find(edge => edge.fromNodeId === sourceId && edge.toNodeId === configId)
      current.connections = existing
        ? current.connections.filter(edge => edge !== existing)
        : [...current.connections, { id: `e_${sourceId}_${configId}`, fromNodeId: sourceId, toNodeId: configId }]
      return current
    })
  }

  const uploadImageNode = async nodeId => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/*'
    input.onchange = async () => {
      const file = input.files && input.files[0]
      if (!file) return
      setBusy('upload')
      try {
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader()
          reader.onload = () => resolve(String(reader.result || ''))
          reader.onerror = () => reject(new Error('读取文件失败'))
          reader.readAsDataURL(file)
        })
        const data = await api.rest('/canvas/assets', { method: 'POST', body: { data_url: dataUrl, name: file.name } })
        patchNode(nodeId, { asset: data.asset, status: 'success' })
      } catch (uploadError) {
        setError(errorText(uploadError))
      } finally {
        setBusy('')
      }
    }
    input.click()
  }

  const generateFrom = async configId => {
    const config = page.nodes.find(node => node.id === configId)
    if (!config) return
    const meta = config.metadata || {}
    const incoming = page.connections.filter(edge => edge.toNodeId === configId).map(edge => edge.fromNodeId)
    const textNodes = page.nodes.filter(node => incoming.includes(node.id) && node.type === 'text')
    const imageNodes = page.nodes.filter(node => incoming.includes(node.id) && node.type === 'image' && node.metadata?.asset?.id)
    const prompt = textNodes
      .map(node => (node.metadata?.text || '').trim())
      .filter(Boolean)
      .join('\n\n')
    if (!prompt) {
      setError('这个配置没有连到任何有文字的提示词节点')
      return
    }
    setBusy(configId)
    setError('')
    try {
      const references = []
      for (const node of imageNodes.slice(0, 5)) {
        const asset = await api.rest(`/canvas/assets/${node.metadata.asset.id}/inline`)
        references.push(asset.data_url)
      }
      const submitted = await api.rest('/jobs', {
        method: 'POST',
        body: {
          channel_id: meta.channelId || '',
          model: meta.model || '',
          mode: references.length ? 'edit' : 'generate',
          prompt,
          size: meta.size || '1:1',
          quality: meta.quality || 'auto',
          detail: '',
          n: meta.count || 1,
          references
        }
      })
      const taskId = submitted.job.id
      patchNode(configId, { status: 'generating', taskId })
      let finalJob = null
      for (let tick = 0; tick < 180; tick += 1) {
        await new Promise(resolve => setTimeout(resolve, 1500))
        const polled = await api.rest(`/jobs/${taskId}`)
        finalJob = polled.job
        if (['succeeded', 'failed', 'cancelled', 'lost'].includes(finalJob.status)) break
      }
      if (!finalJob || finalJob.status !== 'succeeded') {
        patchNode(configId, { status: 'error', error: (finalJob && (finalJob.error || finalJob.status)) || '超时' })
        setError(`生成失败：${(finalJob && finalJob.error) || '未知原因'}`)
        return
      }
      patchNode(configId, { status: 'success', error: '' })
      const baseX = config.x + config.width + 60
      const baseY = config.y
      for (let index = 0; index < (finalJob.images || []).length; index += 1) {
        const image = finalJob.images[index]
        const inline = await api.rest(`/files/${image.id}/inline`)
        const asset = await api.rest('/canvas/assets', { method: 'POST', body: { data_url: inline.data_url, name: `${image.id}.png` } })
        const nodeId = `n_${Math.random().toString(36).slice(2, 10)}`
        update(current => {
          current.nodes.push({
            id: nodeId,
            type: 'image',
            title: `${meta.model || '生成'} #${index + 1}`,
            x: baseX,
            y: baseY + index * 300,
            width: 260,
            height: 260,
            metadata: { asset: asset.asset, status: 'success' }
          })
          current.connections.push({ id: `e_${configId}_${nodeId}`, fromNodeId: configId, toNodeId: nodeId })
          return current
        })
      }
      haptic('success')
    } catch (runError) {
      setError(errorText(runError))
      patchNode(configId, { status: 'error', error: errorText(runError) })
    } finally {
      setBusy('')
    }
  }

  // pointer plumbing ------------------------------------------------------

  useEffect(() => {
    const move = event => {
      if (dragRef.current) {
        const { nodeId, startX, startY, originX, originY, scale } = dragRef.current
        const dx = (event.clientX - startX) / scale
        const dy = (event.clientY - startY) / scale
        update(current => {
          const node = current.nodes.find(entry => entry.id === nodeId)
          if (node) {
            node.x = Math.round(originX + dx)
            node.y = Math.round(originY + dy)
          }
          return current
        })
      } else if (panRef.current) {
        const { startX, startY, originX, originY } = panRef.current
        const dx = event.clientX - startX
        const dy = event.clientY - startY
        update(current => {
          current.viewport = { ...current.viewport, x: originX + dx, y: originY + dy }
          return current
        })
      }
    }
    const up = () => {
      dragRef.current = null
      panRef.current = null
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
  }, [])

  if (!page) {
    return jsxs('div', {
      className: 'flex h-full min-h-0 flex-col gap-2 p-3',
      children: [
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(Button, { size: 'xs', onClick: createPage, disabled: busy === 'create', children: '新建画布' }),
            jsx('span', { className: 'flex-1' }),
            jsx(Button, { size: 'xs', variant: 'ghost', onClick: loadPages, children: '刷新' })
          ]
        }),
        error ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-danger)', children: error }) : null,
        pages.length === 0
          ? jsx(EmptyState, { className: 'flex-1', title: '还没有画布', description: '新建一个：把提示词、生成配置和结果连成一条可迭代的流程。' })
          : jsx('div', {
              className: 'flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto',
              children: pages.map(entry =>
                jsxs('div', {
                  className: 'flex items-center gap-2 rounded border border-(--ui-stroke-secondary) px-2 py-1.5 text-xs',
                  children: [
                    jsx('button', {
                      className: 'truncate text-left font-medium hover:underline',
                      onClick: () => openPage(entry.id),
                      type: 'button',
                      children: entry.title
                    }),
                    jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: `${entry.nodeCount} 个节点 · r${entry.revision} · ${formatStamp(entry.updatedAt)}` }),
                    jsx('span', { className: 'flex-1' }),
                    jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => openPage(entry.id), children: '打开' }),
                    jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => removePage(entry.id), children: '删除' })
                  ]
                }, entry.id)
              )
            })
      ]
    })
  }

  const scale = (page.viewport && page.viewport.k) || 1
  const offsetX = (page.viewport && page.viewport.x) || 0
  const offsetY = (page.viewport && page.viewport.y) || 0
  const textNodes = page.nodes.filter(node => node.type === 'text')
  const imageNodes = page.nodes.filter(node => node.type === 'image')
  const nodeById = new Map(page.nodes.map(node => [node.id, node]))

  return jsxs('div', {
    className: 'relative flex h-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1 border-b border-(--ui-stroke-secondary) px-2 py-1',
        children: [
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setPage(null), children: '← 列表' }),
          jsx(Input, {
            className: 'h-7 w-[180px] text-xs',
            value: page.title,
            onChange: event => update(current => ({ ...current, title: event.target.value }))
          }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => addNode('text'), children: '+ 提示词' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => addNode('config'), children: '+ 配置' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => addNode('image'), children: '+ 图片' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => save(false), children: '保存' }),
          jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: note || `r${page.revision} · ${Math.round(scale * 100)}%` }),
          jsx('span', { className: 'flex-1' }),
          error ? jsx('span', { className: 'max-w-[280px] truncate text-[0.625rem] text-(--ui-danger)', children: error }) : null
        ]
      }),
      jsxs('div', {
        ref: viewportRef,
        className: 'relative min-h-0 flex-1 overflow-hidden bg-(--ui-bg-primary)',
        onPointerDown: event => {
          panRef.current = { startX: event.clientX, startY: event.clientY, originX: offsetX, originY: offsetY }
          setSelected('')
        },
        onWheel: event => {
          event.preventDefault()
          const next = Math.min(3, Math.max(0.2, scale * (event.deltaY > 0 ? 0.92 : 1.08)))
          update(current => ({ ...current, viewport: { ...current.viewport, k: Number(next.toFixed(3)) } }))
        },
        style: {
          backgroundImage: 'radial-gradient(circle, rgba(127,127,127,0.25) 1px, transparent 1px)',
          backgroundSize: `${24 * scale}px ${24 * scale}px`,
          backgroundPosition: `${offsetX}px ${offsetY}px`
        },
        children: [
          jsx('svg', {
            className: 'pointer-events-none absolute inset-0 h-full w-full',
            children: page.connections.map(edge => {
              const from = nodeById.get(edge.fromNodeId)
              const to = nodeById.get(edge.toNodeId)
              if (!from || !to) return null
              const x1 = (from.x + from.width) * scale + offsetX
              const y1 = (from.y + from.height / 2) * scale + offsetY
              const x2 = to.x * scale + offsetX
              const y2 = (to.y + to.height / 2) * scale + offsetY
              const mid = (x1 + x2) / 2
              return jsx('path', {
                d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
                fill: 'none',
                stroke: 'var(--ui-stroke-secondary)',
                strokeWidth: 1.5
              }, edge.id)
            })
          }),
          jsx('div', {
            className: 'absolute',
            style: { left: offsetX, top: offsetY },
            children: page.nodes.map(node =>
              jsx('div', {
                className: 'absolute',
                style: { left: node.x * scale, top: node.y * scale, width: node.width * scale, height: node.height * scale },
                children: jsx(CanvasNodeFrame, {
                  node,
                  scale,
                  selected: selected === node.id,
                  onSelect: setSelected,
                  onDragStart: event => {
                    dragRef.current = { nodeId: node.id, startX: event.clientX, startY: event.clientY, originX: node.x, originY: node.y, scale }
                  },
                  children:
                    node.type === 'text'
                      ? jsx(Textarea, {
                          className: 'h-full w-full resize-none text-[0.6875rem]',
                          placeholder: '写提示词…',
                          value: node.metadata?.text || '',
                          onChange: event => patchNode(node.id, { text: event.target.value })
                        })
                      : node.type === 'image'
                        ? jsxs('div', {
                            className: 'flex min-h-0 flex-1 flex-col gap-1',
                            children: [
                              jsx(CanvasImageView, { asset: node.metadata?.asset, scale }),
                              jsx('div', {
                                className: 'flex items-center justify-between text-[0.625rem] text-(--ui-text-quaternary)',
                                children: [
                                  jsx('span', { className: 'truncate', children: node.metadata?.asset?.name || '' }),
                                  jsxs('div', {
                                    className: 'flex items-center gap-1',
                                    children: [
                                      jsx('button', {
                                        className: 'hover:text-foreground',
                                        onClick: () => openPoster(node),
                                        type: 'button',
                                        children: '海报编辑'
                                      }),
                                      jsx('button', {
                                        className: 'hover:text-foreground',
                                        onClick: () => uploadImageNode(node.id),
                                        type: 'button',
                                        children: '替换'
                                      })
                                    ]
                                  })
                                ]
                              })
                            ]
                          })
                        : jsxs('div', {
                            className: 'flex min-h-0 flex-1 flex-col gap-1 text-[0.625rem]',
                            children: [
                              jsx(Select, {
                                value: node.metadata?.channelId || (settings?.default_channel_id || ''),
                                onValueChange: value => patchNode(node.id, { channelId: value, model: '' }),
                                children: [
                                  jsx(SelectTrigger, { className: 'h-6 text-[0.625rem]', children: jsx(SelectValue, { placeholder: '渠道' }) }),
                                  jsx(SelectContent, {
                                    children: (settings?.channels || []).map(entry =>
                                      jsx(SelectItem, { value: entry.id, children: entry.name }, entry.id)
                                    )
                                  })
                                ]
                              }),
                              jsx(Input, {
                                className: 'h-6 text-[0.625rem]',
                                placeholder: '模型',
                                value: node.metadata?.model || '',
                                onChange: event => patchNode(node.id, { model: event.target.value })
                              }),
                              jsxs('div', {
                                className: 'flex gap-1',
                                children: [
                                  jsx(Input, { className: 'h-6 text-[0.625rem]', value: node.metadata?.size || '', onChange: event => patchNode(node.id, { size: event.target.value }) }),
                                  jsx(Input, { className: 'h-6 text-[0.625rem]', value: node.metadata?.quality || '', onChange: event => patchNode(node.id, { quality: event.target.value }) })
                                ]
                              }),
                              jsx('div', {
                                className: 'min-h-0 flex-1 overflow-y-auto',
                                children: [...textNodes, ...imageNodes].map(source =>
                                  jsxs('label', {
                                    className: 'flex items-center gap-1',
                                    children: [
                                      jsx('input', {
                                        type: 'checkbox',
                                        checked: page.connections.some(edge => edge.fromNodeId === source.id && edge.toNodeId === node.id),
                                        onChange: () => toggleInput(node.id, source.id)
                                      }),
                                      jsx('span', { className: 'truncate', children: `${source.type === 'text' ? '文字' : '图片'}：${source.title || source.id}` })
                                    ]
                                  }, source.id)
                                )
                              }),
                              jsxs('div', {
                                className: 'flex items-center gap-1',
                                children: [
                                  jsx(Button, {
                                    size: 'xs',
                                    disabled: Boolean(busy),
                                    onClick: () => generateFrom(node.id),
                                    children: busy === node.id ? '生成中…' : '生成'
                                  }),
                                  jsx('span', {
                                    className: 'truncate text-(--ui-text-quaternary)',
                                    children: node.metadata?.status === 'error' ? `失败：${node.metadata?.error || ''}` : node.metadata?.status || ''
                                  })
                                ]
                              })
                            ]
                          })
                })
              }, node.id)
            )
          })
        ]
      }),
      posterNode && posterRecipe
        ? jsx(PosterEditor, {
            asset: page.nodes.find(node => node.id === posterNode)?.metadata?.asset,
            initialRecipe: posterRecipe,
            onClose: () => {
              setPosterNode('')
              setPosterRecipe(null)
            },
            onSaved: onPosterSaved
          })
        : null
    ]
  })
}

// ── e-commerce sets ────────────────────────────────────────────────────────

const ECOMMERCE_ROLES = ['product', 'packaging', 'detail', 'style']

function EcommerceView() {
  const draftSource = useValue($draft)
  const [meta, setMeta] = useState({ slots: [], roles: {}, anchorPrefix: '', defaultInfo: '' })
  const [form, setForm] = useState({
    projectName: '',
    productName: '',
    category: '',
    platform: '抖音',
    language: '中文',
    size: '1:1',
    promptInfo: ''
  })
  const [slots, setSlots] = useState([])
  const [assets, setAssets] = useState([]) // {role, name, dataUrl}
  const [planData, setPlanData] = useState(null)
  const [overrides, setOverrides] = useState({})
  const [results, setResults] = useState({}) // key -> {status, images:[{id,dataUrl}], error}
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    api.rest('/ecommerce/slots')
      .then(data => {
        setMeta(data)
        setSlots(data.slots || [])
        setForm(current => ({ ...current, promptInfo: current.promptInfo || data.defaultInfo || '' }))
      })
      .catch(readError => setError(errorText(readError)))
  }, [])

  const patchForm = patch => setForm(current => ({ ...current, ...patch }))

  const addAsset = role => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/*'
    input.onchange = () => {
      const file = input.files && input.files[0]
      if (!file) return
      const reader = new FileReader()
      reader.onload = () => {
        setAssets(current => [...current.filter(entry => entry.role !== role), { role, name: file.name, dataUrl: String(reader.result || '') }])
      }
      reader.readAsDataURL(file)
    }
    input.click()
  }

  const previewPlan = async () => {
    setBusy('plan')
    setError('')
    try {
      const data = await api.rest('/ecommerce/plan', {
        method: 'POST',
        body: { draft: { ...form, slots, promptOverrides: overrides } }
      })
      setPlanData(data)
      setResults([])
      setProgress({ done: 0, total: data.total })
    } catch (planError) {
      setError(errorText(planError))
    } finally {
      setBusy('')
    }
  }

  const assetFor = role => assets.find(entry => entry.role === role)

  const run = async () => {
    if (!planData) return
    if (!draftSource.channelId) {
      setError('先到「渠道」里选一个默认渠道，再回来生成')
      return
    }
    setBusy('run')
    setError('')
    let mainImage = null
    for (let index = 0; index < planData.items.length; index += 1) {
      const item = planData.items[index]
      const key = `${item.slotKey}-${item.index}`
      setProgress({ done: index, total: planData.items.length })
      const reference = assetFor(item.refRole)
      const references = []
      if (mainImage && item.slotKey !== 'main') references.push(mainImage)
      if (reference) references.push(reference.dataUrl)
      const prompt = mainImage && item.slotKey !== 'main' ? `${meta.anchorPrefix || ''}${item.prompt}` : item.prompt
      setResults(current => ({ ...current, [key]: { status: '生成中', images: [] } }))
      let entry = { status: '生成中', images: [] }
      try {
        const submitted = await api.rest('/jobs', {
          method: 'POST',
          body: {
            channel_id: draftSource.channelId,
            model: draftSource.model || '',
            mode: references.length ? 'edit' : 'generate',
            prompt,
            size: planData.size || form.size,
            quality: draftSource.quality || 'auto',
            detail: '',
            n: 1,
            references
          }
        })
        const taskId = submitted.job.id
        let job = null
        for (let tick = 0; tick < 200; tick += 1) {
          await new Promise(resolve => setTimeout(resolve, 1500))
          const polled = await api.rest(`/jobs/${taskId}`)
          job = polled.job
          if (['succeeded', 'failed', 'cancelled', 'lost'].includes(job.status)) break
        }
        if (!job || job.status !== 'succeeded') {
          entry = { status: '失败', images: [], error: (job && (job.error || job.status)) || '超时' }
        } else {
          const images = []
          for (const image of job.images || []) {
            const inline = await api.rest(`/files/${image.id}/inline`)
            images.push({ id: image.id, dataUrl: inline.data_url })
          }
          entry = { status: '完成', images }
          if (item.slotKey === 'main' && images[0]) mainImage = images[0].dataUrl
        }
      } catch (runError) {
        entry = { status: '失败', images: [], error: errorText(runError) }
      }
      setResults(current => ({ ...current, [key]: entry }))
    }
    setProgress({ done: planData.items.length, total: planData.items.length })
    setBusy('')
    haptic('success')
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-2 p-3 text-xs',
    children: [
      jsxs('div', {
        className: 'grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-2',
        children: [
          jsx(Input, { className: 'h-8 text-xs', placeholder: '项目名', value: form.projectName, onChange: event => patchForm({ projectName: event.target.value }) }),
          jsx(Input, { className: 'h-8 text-xs', placeholder: '商品名（一致性锚点）', value: form.productName, onChange: event => patchForm({ productName: event.target.value }) }),
          jsx(Input, { className: 'h-8 text-xs', placeholder: '品类', value: form.category, onChange: event => patchForm({ category: event.target.value }) }),
          jsx(Input, { className: 'h-8 text-xs', placeholder: '平台', value: form.platform, onChange: event => patchForm({ platform: event.target.value }) }),
          jsx(Input, { className: 'h-8 text-xs', placeholder: '语言（中文 / English / custom）', value: form.language, onChange: event => patchForm({ language: event.target.value }) }),
          jsx(Input, { className: 'h-8 text-xs', placeholder: '比例 1:1', value: form.size, onChange: event => patchForm({ size: event.target.value }) })
        ]
      }),
      jsx(Textarea, {
        className: 'h-20 w-full resize-none text-xs',
        placeholder: '商品信息与要求（材质、卖点、包装文字、禁忌…）',
        value: form.promptInfo,
        onChange: event => patchForm({ promptInfo: event.target.value })
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('span', { className: 'text-(--ui-text-tertiary)', children: '参考图：' }),
          ...ECOMMERCE_ROLES.map(role => {
            const asset = assetFor(role)
            return jsxs('span', {
              className: 'flex items-center gap-1 rounded border border-(--ui-stroke-secondary) px-1.5 py-0.5',
              children: [
                jsx('span', { children: `${meta.roles?.[role] || role}` }),
                jsx('button', { className: 'text-(--ui-accent)', onClick: () => addAsset(role), type: 'button', children: asset ? '✓ 替换' : '上传' })
              ]
            }, role)
          }),
          jsx('span', { className: 'flex-1' }),
          jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy === 'plan', onClick: previewPlan, children: busy === 'plan' ? '生成计划中…' : '预览套图计划' })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2 rounded border border-(--ui-stroke-secondary) p-1.5',
        children: slots.map(slot =>
          jsxs('label', {
            className: 'flex items-center gap-1',
            children: [
              jsx('input', {
                type: 'checkbox',
                checked: Boolean(slot.enabled),
                onChange: event => setSlots(current => current.map(entry => (entry.key === slot.key ? { ...entry, enabled: event.target.checked } : entry)))
              }),
              jsx('span', { children: `${slot.label}×${slot.count}` }),
              jsx('span', { className: 'text-(--ui-text-quaternary)', children: slot.description })
            ]
          }, slot.key)
        )
      }),
      error ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-danger)', children: error }) : null,
      planData
        ? jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Button, { size: 'xs', disabled: Boolean(busy), onClick: run, children: busy === 'run' ? `生成中 ${progress.done}/${progress.total}` : `开始生成 ${planData.total} 张` }),
              jsx('span', { className: 'text-(--ui-text-quaternary)', children: `计划：${planData.total} 张 · ${planData.projectName} · 比例 ${planData.size}` }),
              jsx('span', { className: 'flex-1' }),
              jsx('Button', { size: 'xs', variant: 'ghost', onClick: () => { setPlanData(null); setResults([]) }, children: '清空计划' })
            ]
          })
        : null,
      jsx('div', {
        className: 'min-h-0 flex-1 overflow-y-auto',
        children: planData
          ? jsxs('div', {
              className: 'grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-2',
              children: planData.items.map((item, order) => {
                const key = `${item.slotKey}-${item.index}`
                const done = results[key]
                return jsxs('div', {
                  className: 'flex flex-col gap-1 rounded border border-(--ui-stroke-secondary) p-1.5',
                  children: [
                    jsxs('div', {
                      className: 'flex items-center justify-between text-[0.625rem]',
                      children: [
                        jsx('span', { children: `${order + 1}. ${item.slotLabel} #${item.index + 1}` }),
                        jsx('span', { className: done ? (done.status === '失败' ? 'text-(--ui-danger)' : 'text-(--ui-text-tertiary)') : 'text-(--ui-text-quaternary)', children: done ? done.status : '待生成' })
                      ]
                    }),
                    jsx(Textarea, {
                      className: 'h-24 w-full resize-none text-[0.625rem]',
                      value: overrides[key] ?? item.prompt,
                      onChange: event => setOverrides(current => ({ ...current, [key]: event.target.value }))
                    }),
                    done && done.images.length
                      ? jsx('img', { alt: item.slotLabel, className: 'w-full rounded object-contain', src: done.images[0].dataUrl })
                      : null,
                    done && done.error ? jsx('div', { className: 'text-[0.625rem] text-(--ui-danger)', children: done.error }) : null
                  ]
                }, key)
              })
            })
          : jsx(EmptyState, {
              className: 'flex-1',
              title: '先预览套图计划',
              description: '填商品信息、勾选要出的图，点「预览套图计划」：每张图的提示词都能改，再一键批量生成。'
            })
      })
    ]
  })
}

// ── multi-model comparison ────────────────────────────────────────────────

function CompareView() {
  const draft = useValue($draft)
  const settings = useValue($settings)
  const references = useValue($references)
  const mask = useValue($mask)
  const channel = activeChannel(settings, draft.channelId)
  const models = (channel && channel.models) || []
  const [picked, setPicked] = useState([])
  const [cards, setCards] = useState({})
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    setPicked(current => current.filter(model => models.includes(model)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models.join('|')])

  const toggle = model =>
    setPicked(current => (current.includes(model) ? current.filter(entry => entry !== model) : [...current, model]))

  const runOne = async model => {
    const started = Date.now()
    setCards(current => ({ ...current, [model]: { status: '生成中', images: [] } }))
    try {
      const body = {
        channel_id: draft.channelId,
        model,
        mode: draft.mode,
        prompt: draft.prompt,
        size: draft.size,
        quality: draft.quality,
        n: 1,
        references: draft.mode === 'edit' ? references.map(reference => reference.dataUrl) : [],
        mask: draft.mode === 'edit' ? mask : ''
      }
      const submitted = await api.rest('/jobs', { method: 'POST', body, timeoutMs: 30000 })
      const taskId = submitted.job.id
      let job = null
      for (let tick = 0; tick < 200; tick += 1) {
        await new Promise(resolve => setTimeout(resolve, 1500))
        const polled = await api.rest(`/jobs/${taskId}`)
        job = polled.job
        if (['succeeded', 'failed', 'cancelled', 'lost'].includes(job.status)) break
      }
      if (!job || job.status !== 'succeeded') {
        setCards(current => ({
          ...current,
          [model]: { status: '失败', images: [], error: (job && (job.error || job.status)) || '超时', elapsed: Date.now() - started }
        }))
        return
      }
      const images = []
      for (const image of job.images || []) {
        const inline = await api.rest(`/files/${image.id}/inline`)
        images.push({ id: image.id, dataUrl: inline.data_url })
      }
      setCards(current => ({ ...current, [model]: { status: '完成', images, elapsed: Date.now() - started, jobId: taskId } }))
    } catch (runError) {
      setCards(current => ({ ...current, [model]: { status: '失败', images: [], error: errorText(runError), elapsed: Date.now() - started } }))
    }
  }

  const run = async () => {
    if (!draft.channelId) {
      setError('先在「工作室」里选一个渠道')
      return
    }
    if (!draft.prompt.trim()) {
      setError('提示词不能为空：在「工作室」里写好再来对比')
      return
    }
    if (picked.length === 0) {
      setError('至少勾选一个模型')
      return
    }
    setError('')
    setRunning(true)
    await Promise.all(picked.map(model => runOne(model)))
    setRunning(false)
    haptic('success')
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-2 p-3 text-xs',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('span', { className: 'font-medium', children: '同一提示词，投给多个模型' }),
          jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: `渠道：${channel ? channel.name : '未选'} · 模式：${draft.mode === 'edit' ? '图生图' : '文生图'} · 比例 ${draft.size} · 清晰度 ${draft.quality}` }),
          jsx('span', { className: 'flex-1' }),
          jsx(Button, { size: 'xs', disabled: running || picked.length === 0, onClick: run, children: running ? '对比中…' : `对比生成（${picked.length}）` }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setCards({}), children: '清空结果' })
        ]
      }),
      jsx('div', {
        className: 'min-h-0 shrink-0 overflow-y-auto rounded border border-(--ui-stroke-secondary) p-1.5',
        children:
          models.length === 0
            ? jsx('div', { className: 'text-(--ui-text-quaternary)', children: '这个渠道还没有模型：去「工作室 → 渠道」点检测模型' })
            : jsxs('div', {
                className: 'flex flex-wrap gap-2',
                children: models.map(model =>
                  jsxs('label', {
                    className: 'flex items-center gap-1',
                    children: [
                      jsx('input', { type: 'checkbox', checked: picked.includes(model), onChange: () => toggle(model) }),
                      jsx('span', { children: model })
                    ]
                  }, model)
                )
              })
      }),
      error ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-danger)', children: error }) : null,
      jsx('div', {
        className: 'min-h-0 flex-1 overflow-y-auto',
        children:
          picked.length === 0
            ? jsx(EmptyState, {
                className: 'flex-1',
                title: '勾选要对比的模型',
                description: '提示词、参考图、遮罩都沿用工作室里当前的内容；每个模型各出一张，可以单独重试。'
              })
            : jsx('div', {
                className: 'grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-2',
                children: picked.map(model => {
                  const card = cards[model]
                  return jsxs('div', {
                    className: 'flex flex-col gap-1 rounded border border-(--ui-stroke-secondary) p-1.5',
                    children: [
                      jsxs('div', {
                        className: 'flex items-center justify-between text-[0.625rem]',
                        children: [
                          jsx('span', { className: 'truncate font-medium', children: model }),
                          jsx('span', {
                            className: card && card.status === '失败' ? 'text-(--ui-danger)' : 'text-(--ui-text-quaternary)',
                            children: card
                              ? `${card.status}${card.elapsed ? ` · ${(card.elapsed / 1000).toFixed(1)}s` : ''}`
                              : '待生成'
                          })
                        ]
                      }),
                      card && card.images.length
                        ? jsx('img', { alt: model, className: 'w-full rounded object-contain', src: card.images[0].dataUrl })
                        : jsx('div', {
                            className: 'grid aspect-square w-full place-items-center rounded bg-(--ui-bg-tertiary) text-(--ui-text-quaternary)',
                            children: card && card.status === '生成中' ? '生成中…' : card && card.error ? card.error : '—'
                          }),
                      jsxs('div', {
                        className: 'flex items-center gap-1',
                        children: [
                          jsx(Button, {
                            size: 'xs',
                            variant: 'ghost',
                            disabled: running,
                            onClick: () => {
                              setRunning(true)
                              runOne(model).finally(() => setRunning(false))
                            },
                            children: '重试'
                          }),
                          card && card.images[0]
                            ? jsx('a', {
                                className: 'text-(--ui-accent) hover:underline',
                                download: `${model}.png`,
                                href: card.images[0].dataUrl,
                                children: '下载'
                              })
                            : null,
                          jsx(Button, {
                            size: 'xs',
                            variant: 'ghost',
                            onClick: () => {
                              patchDraft({ model })
                              $view.set('studio')
                            },
                            children: '用它'
                          })
                        ]
                      })
                    ]
                  }, model)
                })
              })
      })
    ]
  })
}

// ── page ───────────────────────────────────────────────────────────────────

const VIEWS = [
  { id: 'studio', label: '工作室' },
  { id: 'compare', label: '对比' },
  { id: 'ecommerce', label: '套图' },
  { id: 'templates', label: '模板' },
  { id: 'canvas', label: '画布' },
  { id: 'history', label: '历史' },
  { id: 'channels', label: '渠道' }
]

function ImageGenPage() {
  const view = useValue($view)
  const version = useValue($version)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let alive = true
    loadBootstrap().finally(() => {
      if (alive) setReady(true)
    })
    return () => {
      alive = false
    }
  }, [])

  const body =
    view === 'channels'
      ? jsx(ChannelsView, {})
      : view === 'templates'
        ? jsx(TemplatesView, {})
        : view === 'canvas'
          ? jsx(CanvasView, {})
          : view === 'ecommerce'
            ? jsx(EcommerceView, {})
            : view === 'compare'
              ? jsx(CompareView, {})
              : view === 'history'
              ? jsx(HistoryView, {})
              : jsx(StudioView, {})

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-1.5',
        children: [
          jsx('div', { className: 'text-sm font-medium', children: '生图' }),
          jsx(SegmentedControl, {
            onChange: next => $view.set(next),
            options: VIEWS,
            sizing: 'content',
            value: view
          }),
          jsx('span', { className: 'flex-1' }),
          version
            ? jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: `v${version}` })
            : null
        ]
      }),
      jsx('div', {
        className: 'min-h-0 flex-1',
        children: ready ? body : jsx('div', { className: 'p-4', children: jsx(GlyphSpinner, {}) })
      })
    ]
  })
}

// ── plugin ─────────────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'ImageGen',
  defaultEnabled: false,
  register(ctx) {
    api = ctx

    const savedDraft = ctx.storage.get(STORAGE_DRAFT, null)
    if (savedDraft && typeof savedDraft === 'object') {
      $draft.set({ ...$draft.get(), ...savedDraft })
    }

    ctx.register({
      id: 'page',
      area: 'routes',
      title: '生图',
      data: { path: ROUTE },
      render: () => jsx(ImageGenPage, {})
    })

    ctx.register({
      id: 'nav',
      area: 'sidebar.nav',
      data: { path: ROUTE, label: '生图', codicon: 'file-media' }
    })

    ctx.register({
      id: 'palette',
      area: PALETTE_AREA,
      data: {
        id: `${ID}.open`,
        label: '生图：打开工作台',
        keywords: ['image', 'imagegen', 'draw', 'draw 生图'],
        run: () => host.navigate(ROUTE)
      }
    })

    ctx.onDispose(() => {
      api = null
    })
  }
}
