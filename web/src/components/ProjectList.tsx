import { useMemo, useState } from 'react'
import type { Project } from '../lib/api'

interface Props {
  projects: Project[]
  selected: string | null
  onSelect: (name: string) => void
}

function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

const ALL = 'all'

type OutputFilter = 'all' | 'report' | 'no-report' | 'materials'

export default function ProjectList({ projects, selected, onSelect }: Props) {
  const [query, setQuery] = useState('')
  const [origin, setOrigin] = useState<string>(ALL)
  const [output, setOutput] = useState<OutputFilter>(ALL)

  // Origins come from the data, not a hardcoded list: a new surface (or a
  // custom RESEARCHKIT_ORIGIN batch label) shows up without a frontend change.
  const origins = useMemo(
    () => [...new Set(projects.map((p) => p.origin || 'unknown'))].sort(),
    [projects],
  )

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return [...projects]
      .filter((p) => origin === ALL || (p.origin || 'unknown') === origin)
      .filter((p) => {
        if (output === 'report') return p.has_report
        if (output === 'no-report') return !p.has_report
        if (output === 'materials') return Boolean(p.has_materials)
        return true
      })
      .filter(
        (p) =>
          !needle ||
          p.topic.toLowerCase().includes(needle) ||
          p.name.toLowerCase().includes(needle) ||
          String(p.origin_pid ?? '').includes(needle),
      )
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
  }, [projects, query, origin, output])

  const selectClass =
    'rounded-md border border-neutral-300 bg-transparent px-1.5 py-1 text-xs ' +
    'dark:border-neutral-700'

  return (
    <nav aria-label="Past projects">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Past projects
      </h2>

      <div className="mb-2 space-y-1.5">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter by topic, folder or pid"
          aria-label="Filter projects"
          className="w-full rounded-md border border-neutral-300 bg-transparent px-2 py-1
            text-sm dark:border-neutral-700"
        />
        <div className="flex gap-1.5">
          <select
            value={origin}
            onChange={(e) => setOrigin(e.target.value)}
            aria-label="Filter by origin"
            className={selectClass}
          >
            <option value={ALL}>All origins</option>
            {origins.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
          <select
            value={output}
            onChange={(e) => setOutput(e.target.value as OutputFilter)}
            aria-label="Filter by output"
            className={selectClass}
          >
            <option value={ALL}>Any output</option>
            <option value="report">Has report</option>
            <option value="no-report">No report</option>
            <option value="materials">Has materials</option>
          </select>
        </div>
      </div>

      {projects.length === 0 ? (
        <p className="text-sm text-neutral-500">No projects yet.</p>
      ) : visible.length === 0 ? (
        <p className="text-sm text-neutral-500">No projects match these filters.</p>
      ) : (
        <>
          <p className="mb-1 text-xs text-neutral-500">
            {visible.length} of {projects.length}
          </p>
          <ul className="space-y-1">
            {visible.map((p) => (
              <li key={p.name}>
                <button
                  type="button"
                  onClick={() => onSelect(p.name)}
                  aria-current={p.name === selected || undefined}
                  className={`w-full rounded-lg px-3 py-2 text-left text-sm hover:bg-neutral-100
                    dark:hover:bg-neutral-800 ${
                      p.name === selected ? 'bg-orange-50 dark:bg-orange-950/40' : ''
                    }`}
                >
                  <span className="block truncate font-medium">{p.topic}</span>
                  <span className="block text-xs text-neutral-500">
                    {formatDate(p.created_at)} · {p.days}d · {p.providers.length} providers
                    {!p.has_report && ' · no report'}
                  </span>
                  <span className="block text-xs text-neutral-400">
                    {p.origin || 'unknown'}
                    {p.origin_pid ? ` · pid ${p.origin_pid}` : ''}
                    {p.preset ? ` · ${p.preset}` : ''}
                    {p.has_materials && ' · materials'}
                    {p.has_article && ' · article'}
                    {p.has_skill && ' · skill'}
                    {p.has_brain && ' · brain'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </nav>
  )
}
