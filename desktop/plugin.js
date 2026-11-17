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
  atom,
  cn,
  haptic,
  host,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
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
  size: '1:1',
  quality: '2k',
  n: 1,
  prompt: ''
})
const $references = atom([])
const $job = atom(null)
const $error = atom('')
const $busy = atom(false)

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
      references: draft.mode === 'edit' ? references.map(reference => reference.dataUrl) : []
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

function ReferenceStrip() {
  const references = useValue($references)
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
          jsx('span', { children: `参考图 ${references.length}/5` }),
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
      })
    ]
  })
}

// ── studio ─────────────────────────────────────────────────────────────────

function StudioView() {
  const settings = useValue($settings)
  const draft = useValue($draft)
  const job = useValue($job)
  const error = useValue($error)
  const running = isRunning(job)
  const channels = (settings && settings.channels) || []
  const channel = activeChannel(settings, draft.channelId)
  const models = (channel && channel.models) || []

  return jsx('div', {
    className: 'flex h-full min-h-0',
    children: [
      jsxs('div', {
        className: 'flex w-[336px] shrink-0 flex-col gap-2.5 overflow-y-auto border-r border-(--ui-stroke-secondary) p-3',
        children: [
          jsxs('div', {
            className: 'flex items-center justify-between',
            children: [
              jsx('div', { className: 'text-sm font-medium', children: '生图' }),
              jsx(Button, {
                size: 'xs',
                variant: 'ghost',
                onClick: () => $view.set('channels'),
                children: '渠道设置'
              })
            ]
          }),
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
                : jsx('div', {
                    className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: '该渠道还没有模型，去「渠道设置」点「检测模型」'
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
              jsx('button', {
                className: 'hover:text-foreground',
                onClick: () => patchDraft({ prompt: '' }),
                type: 'button',
                children: '清空提示词'
              })
            ]
          }),
          draft.mode === 'edit' ? jsx(ReferenceStrip, {}) : null,
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
        ]
      }),
      jsx('div', { className: 'min-w-0 flex-1 overflow-y-auto p-4', children: jsx(ResultPanel, {}) })
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
    setDetected([])
    setStatus('')
    setForm({
      id: channel.id,
      name: channel.name || '',
      api_url: channel.api_url || '',
      api_key: '',
      api_key_env: channel.api_key_env || '',
      api_url_full: Boolean(channel.api_url_full),
      protocol: channel.protocol || 'auto',
      clear_api_key: false
    })
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
                        jsx('span', { children: `${(channel.models || []).length} 个模型` }),
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
                className: 'mt-4 flex max-w-[560px] flex-wrap gap-1',
                children: [
                  jsx('div', { className: 'w-full text-[0.6875rem] text-(--ui-text-tertiary)', children: '探测到的生图模型' }),
                  ...detected.map(model =>
                    jsx(Badge, { children: `${model.id}${model.known ? '' : '（未知协议）'}` }, model.id)
                  )
                ]
              })
            : null
        ]
      })
    ]
  })
}

// ── page ───────────────────────────────────────────────────────────────────

function ImageGenPage() {
  const view = useValue($view)
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

  return jsx('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: ready ? (view === 'channels' ? jsx(ChannelsView, {}) : jsx(StudioView, {})) : jsx('div', { className: 'p-4', children: jsx(GlyphSpinner, {}) })
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
