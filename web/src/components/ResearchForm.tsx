import { useState, type FormEvent } from 'react'
import type { ConfigResponse, ResearchRequest } from '../lib/api'

const DAY_OPTIONS = [1, 3, 7, 14, 30]

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

  function toggleProvider(name: string) {
    setProviders((prev) =>
      prev.includes(name) ? prev.filter((p) => p !== name) : [...prev, name],
    )
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!topic.trim() || providers.length === 0) return
    onSubmit({
      topic: topic.trim(),
      days,
      providers,
      preset,
      // ponytail: sources fixed to the tool's default pair; add a control when the API grows more.
      sources: ['social', 'web'],
    })
  }

  const inputClass =
    'w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm ' +
    'focus:border-indigo-500 focus:outline-none dark:border-neutral-700 dark:bg-neutral-900'

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label htmlFor="topic" className="mb-1 block text-sm font-medium">
          Topic
        </label>
        <textarea
          id="topic"
          required
          rows={3}
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          placeholder="e.g. What are people saying about AI agents in production?"
          className={inputClass}
        />
      </div>

      <div className="flex flex-wrap gap-4">
        <div>
          <label htmlFor="days" className="mb-1 block text-sm font-medium">
            Days back
          </label>
          <select
            id="days"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className={inputClass}
          >
            {DAY_OPTIONS.map((d) => (
              <option key={d} value={d}>
                {d} {d === 1 ? 'day' : 'days'}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="preset" className="mb-1 block text-sm font-medium">
            Preset
          </label>
          <select
            id="preset"
            value={preset}
            onChange={(e) => setPreset(e.target.value)}
            className={inputClass}
          >
            {config.presets.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
      </div>

      <fieldset>
        <legend className="mb-1 text-sm font-medium">Providers</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {config.providers.map((p) => (
            <label key={p} className="flex items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                checked={providers.includes(p)}
                onChange={() => toggleProvider(p)}
                className="accent-indigo-600"
              />
              {p}
            </label>
          ))}
        </div>
        {providers.length === 0 && (
          <p className="mt-1 text-xs text-red-600 dark:text-red-400">
            Select at least one provider.
          </p>
        )}
      </fieldset>

      <button
        type="submit"
        disabled={running || !topic.trim() || providers.length === 0}
        className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white
          hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {running ? 'Researching…' : 'Start research'}
      </button>
    </form>
  )
}
