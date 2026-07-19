import { useRef, useState, type FormEvent } from 'react'
import {
  improveTopic,
  setActivePreset,
  type ConfigResponse,
  type ResearchRequest,
  type UserFile,
} from '../lib/api'

const MAX_DAYS = 365
const DAY_PRESETS = [7, 30, 90, 180, 365]

const SOURCE_TABS = ['Files', 'Links', 'Text'] as const
type SourceTab = (typeof SOURCE_TABS)[number]

const HARNESS_PROVIDERS = ['openai', 'gemini', 'grok', 'claude', 'kimi']

const FILE_PATTERN = /\.(md|markdown|txt|rst)$/i
const MAX_FILES = 10
const MAX_FILE_BYTES = 200 * 1024
const MAX_TEXT_CHARS = 100_000

// ponytail: FileReader over file.text() — same result, also works in jsdom.
function readFileText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = () => reject(reader.error ?? new Error(`Failed to read ${file.name}`))
    reader.readAsText(file)
  })
}

const toggle = (name: string) => (prev: string[]) =>
  prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name]

const splitLines = (value: string) =>
  value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)

interface Props {
  config: ConfigResponse
  running: boolean
  onSubmit: (request: ResearchRequest) => void
}

export default function ResearchForm({ config, running, onSubmit }: Props) {
  const [topic, setTopic] = useState('')
  const [days, setDays] = useState(7)
  const [providers, setProviders] = useState<string[]>(config.default_providers)
  const [preset, setPreset] = useState(config.active_preset)
  const [boost, setBoost] = useState(false)
  const [harness, setHarness] = useState(false)
  const [includeRaw, setIncludeRaw] = useState(true)
  const [siteResearch, setSiteResearch] = useState(true)
  const [sites, setSites] = useState<string[]>(config.default_sites)
  const [improving, setImproving] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const [sourceTab, setSourceTab] = useState<SourceTab>('Files')
  const [files, setFiles] = useState<UserFile[]>([])
  const [fileError, setFileError] = useState<string | null>(null)
  const [userUrls, setUserUrls] = useState('')
  const [userText, setUserText] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const daysValid = Number.isFinite(days) && days >= 1 && days <= MAX_DAYS
  const sourcesDisabled = boost || running
  const harnessAvailable = config.presets.includes('harness')
  // ponytail: harness overrides are derived, not stored — toggling off restores prior selections.
  const effectiveProviders = harness ? HARNESS_PROVIDERS : providers
  const effectiveSiteResearch = harness ? false : siteResearch

  async function handleImprove() {
    setImproving(true)
    setActionError(null)
    try {
      setTopic((await improveTopic(topic.trim())).topic)
    } catch {
      setActionError('Failed to improve the topic.')
    } finally {
      setImproving(false)
    }
  }

  async function handlePresetChange(next: string) {
    setPreset(next)
    setActionError(null)
    try {
      setPreset((await setActivePreset(next)).active_preset)
    } catch {
      setActionError('Failed to switch the preset.')
    }
  }

  async function addFiles(incoming: File[]) {
    const problems: string[] = []
    const accepted: UserFile[] = []
    let room = MAX_FILES - files.length
    for (const file of incoming) {
      if (!FILE_PATTERN.test(file.name)) {
        problems.push(`${file.name}: only .md, .markdown, .txt, .rst files are accepted.`)
        continue
      }
      if (file.size > MAX_FILE_BYTES) {
        problems.push(`${file.name}: larger than 200 KB.`)
        continue
      }
      if (room <= 0) {
        problems.push(`Limit of ${MAX_FILES} files reached.`)
        break
      }
      try {
        accepted.push({ name: file.name, content: await readFileText(file) })
        room--
      } catch {
        problems.push(`${file.name}: could not be read.`)
      }
    }
    if (accepted.length > 0) setFiles((prev) => [...prev, ...accepted])
    setFileError(problems.length > 0 ? problems.join(' ') : null)
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!topic.trim() || effectiveProviders.length === 0 || !daysValid) return
    onSubmit({
      topic: topic.trim(),
      days,
      providers: effectiveProviders,
      preset: harness ? 'harness' : preset,
      // ponytail: sources fixed to the tool's default pair; add a control when the API grows more.
      sources: ['social', 'web'],
      include_raw: includeRaw,
      site_research: effectiveSiteResearch,
      // ponytail: always send the explicit selection; server defaults never have to guess.
      site_research_sites: sites,
      boost,
      user_files: files,
      user_urls: splitLines(userUrls),
      user_texts: userText.trim() ? [userText.slice(0, MAX_TEXT_CHARS)] : [],
    })
  }

  const inputClass =
    'rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm ' +
    'focus:border-orange-500 focus:outline-none disabled:opacity-50 ' +
    'dark:border-neutral-700 dark:bg-neutral-900'
  const smallButtonClass =
    'rounded-lg border border-neutral-300 px-2.5 py-1 text-xs font-medium hover:bg-neutral-100 ' +
    'disabled:cursor-not-allowed disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800'

  const sourceCounts: Record<SourceTab, number> = {
    Files: files.length,
    Links: splitLines(userUrls).length,
    Text: userText.trim() ? 1 : 0,
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <div className="mb-1 flex items-center justify-between">
          <label htmlFor="topic" className="text-sm font-medium">
            Topic
          </label>
          <button
            type="button"
            onClick={() => void handleImprove()}
            disabled={improving || running || !topic.trim()}
            className={smallButtonClass}
          >
            {improving ? 'Improving…' : 'Improve topic'}
          </button>
        </div>
        <textarea
          id="topic"
          required
          rows={3}
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          placeholder="e.g. What are people saying about AI agents in production?"
          className={`w-full ${inputClass}`}
        />
      </div>

      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <label htmlFor="days" className="mb-1 block text-sm font-medium">
            Days
          </label>
          <div className="flex flex-wrap items-center gap-1.5">
            <input
              id="days"
              type="number"
              required
              min={1}
              max={MAX_DAYS}
              value={Number.isNaN(days) ? '' : days}
              onChange={(e) => setDays(e.target.valueAsNumber)}
              className={`w-24 ${inputClass}`}
            />
            {DAY_PRESETS.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => setDays(d)}
                aria-pressed={days === d}
                className={`rounded-full px-2.5 py-1 text-xs font-medium ${
                  days === d
                    ? 'bg-orange-600 text-white'
                    : 'border border-neutral-300 text-neutral-600 hover:bg-neutral-100 ' +
                      'dark:border-neutral-700 dark:text-neutral-400 dark:hover:bg-neutral-800'
                }`}
              >
                {d}
              </button>
            ))}
          </div>
          <p className="mt-1 text-xs text-neutral-500">lookback window, up to 365</p>
        </div>
        <div className="w-36 shrink-0">
          <label htmlFor="preset" className="mb-1 block text-sm font-medium">
            Preset
          </label>
          <select
            id="preset"
            value={harness ? 'harness' : preset}
            disabled={harness}
            onChange={(e) => void handlePresetChange(e.target.value)}
            className={`w-full ${inputClass}`}
          >
            {config.presets.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
      </div>

      {harnessAvailable && (
        <div>
          <label className="flex items-center gap-1.5 text-sm font-medium">
            <input
              type="checkbox"
              checked={harness}
              onChange={(e) => setHarness(e.target.checked)}
              className="accent-orange-600"
            />
            Harness only (no API keys)
          </label>
          <p className="mt-1 text-xs text-neutral-500">
            Runs on logged-in CLI subscriptions (Claude Code, Codex, Antigravity, Grok CLI).
          </p>
        </div>
      )}

      <fieldset>
        <legend className="mb-1 text-sm font-medium">Providers</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {config.providers.map((p) => (
            <label key={p} className="flex items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                checked={effectiveProviders.includes(p)}
                disabled={harness}
                onChange={() => setProviders(toggle(p))}
                className="accent-orange-600"
              />
              {p}
            </label>
          ))}
        </div>
        {effectiveProviders.length === 0 && (
          <p className="mt-1 text-xs text-red-600 dark:text-red-400">
            Select at least one provider.
          </p>
        )}
      </fieldset>

      <div>
        <label className="flex items-center gap-1.5 text-sm font-medium">
          <input
            type="checkbox"
            checked={boost}
            onChange={(e) => setBoost(e.target.checked)}
            className="accent-orange-600"
          />
          Boost (LLM council)
          {harness && (
            <span className="text-xs font-normal text-neutral-500">
              (recommended with harness mode)
            </span>
          )}
        </label>
        <p className="mt-1 text-xs text-neutral-500">
          Council refines the topic and may fan out into parallel sub-projects with a super-summary.
          Custom sources are ignored.
        </p>
      </div>

      <div className="flex flex-wrap gap-x-6 gap-y-2">
        <label className="flex items-center gap-1.5 text-sm">
          <input
            type="checkbox"
            checked={includeRaw}
            onChange={(e) => setIncludeRaw(e.target.checked)}
            className="accent-orange-600"
          />
          Include raw provider outputs
        </label>
        <label className="flex items-center gap-1.5 text-sm">
          <input
            type="checkbox"
            checked={effectiveSiteResearch}
            disabled={harness}
            onChange={(e) => setSiteResearch(e.target.checked)}
            className="accent-orange-600"
          />
          Site research
        </label>
      </div>

      {effectiveSiteResearch && config.connectors.length > 0 && (
        <fieldset className="ml-6">
          <legend className="mb-1 text-sm font-medium">Sites</legend>
          <div className="flex flex-wrap gap-x-4 gap-y-2">
            {config.connectors.map((c) => (
              <label key={c} className="flex items-center gap-1.5 text-sm">
                <input
                  type="checkbox"
                  checked={sites.includes(c)}
                  onChange={() => setSites(toggle(c))}
                  className="accent-orange-600"
                />
                {c}
              </label>
            ))}
          </div>
        </fieldset>
      )}

      <div>
        <p className="mb-1 text-sm font-medium">Custom sources</p>
        <div className="mb-2 inline-flex gap-0.5 rounded-lg border border-neutral-300 p-0.5 dark:border-neutral-700">
          {SOURCE_TABS.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setSourceTab(t)}
              disabled={sourcesDisabled}
              aria-pressed={t === sourceTab}
              className={`rounded-md px-3 py-1 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${
                t === sourceTab
                  ? 'bg-orange-600 text-white'
                  : 'text-neutral-500 hover:text-neutral-900 dark:hover:text-neutral-100'
              }`}
            >
              {t}
              {sourceCounts[t] > 0 ? ` (${sourceCounts[t]})` : ''}
            </button>
          ))}
        </div>

        {sourceTab === 'Files' && (
          <div>
            <div
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault()
                if (!sourcesDisabled) void addFiles(Array.from(e.dataTransfer.files))
              }}
              className="rounded-lg border-2 border-dashed border-neutral-300 p-4 text-center
                text-sm text-neutral-500 dark:border-neutral-700"
            >
              Drag documents here, or{' '}
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={sourcesDisabled}
                className="font-medium text-orange-600 underline disabled:cursor-not-allowed
                  disabled:opacity-50 dark:text-orange-400"
              >
                browse
              </button>
              <p className="mt-1 text-xs">.md, .markdown, .txt, .rst — max 10 files, 200 KB each</p>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept=".md,.markdown,.txt,.rst"
                aria-label="Add files"
                disabled={sourcesDisabled}
                className="hidden"
                onChange={(e) => {
                  void addFiles(Array.from(e.target.files ?? []))
                  e.target.value = ''
                }}
              />
            </div>
            {fileError && (
              <p role="alert" className="mt-1 text-xs text-red-600 dark:text-red-400">
                {fileError}
              </p>
            )}
            {files.length > 0 && (
              <ul className="mt-2 space-y-1">
                {files.map((f, i) => (
                  <li
                    key={`${f.name}-${i}`}
                    className="flex items-center justify-between rounded-lg bg-neutral-100 px-3
                      py-1.5 text-sm dark:bg-neutral-800"
                  >
                    <span className="min-w-0 truncate">
                      {f.name}{' '}
                      <span className="text-xs text-neutral-500">
                        ({Math.max(1, Math.round(f.content.length / 1024))} KB)
                      </span>
                    </span>
                    <button
                      type="button"
                      onClick={() => setFiles(files.filter((_, idx) => idx !== i))}
                      disabled={sourcesDisabled}
                      aria-label={`Remove ${f.name}`}
                      className="ml-2 text-neutral-500 hover:text-red-600"
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {sourceTab === 'Links' && (
          <div>
            <label htmlFor="urls" className="mb-1 block text-xs text-neutral-500">
              URLs (one per line)
            </label>
            <textarea
              id="urls"
              rows={3}
              value={userUrls}
              onChange={(e) => setUserUrls(e.target.value)}
              disabled={sourcesDisabled}
              placeholder="https://example.com/post"
              className={`w-full ${inputClass}`}
            />
          </div>
        )}

        {sourceTab === 'Text' && (
          <div>
            <label htmlFor="freeform" className="mb-1 block text-xs text-neutral-500">
              Free-form context
            </label>
            <textarea
              id="freeform"
              rows={4}
              maxLength={MAX_TEXT_CHARS}
              value={userText}
              onChange={(e) => setUserText(e.target.value)}
              disabled={sourcesDisabled}
              placeholder="Paste context…"
              className={`w-full ${inputClass}`}
            />
          </div>
        )}
      </div>

      {actionError && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {actionError}
        </p>
      )}

      <button
        type="submit"
        disabled={running || !topic.trim() || effectiveProviders.length === 0 || !daysValid}
        className="rounded-lg bg-orange-600 px-4 py-2 text-sm font-medium text-white
          hover:bg-orange-500 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {running ? 'Researching…' : 'Start research'}
      </button>
    </form>
  )
}
