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

export default function ProjectList({ projects, selected, onSelect }: Props) {
  const sorted = [...projects].sort((a, b) => b.created_at.localeCompare(a.created_at))

  return (
    /* ponytail: <details open> = collapsible on mobile, plain sidebar on desktop. No JS state. */
    <details open className="group">
      <summary
        className="mb-2 cursor-pointer list-none text-sm font-semibold uppercase tracking-wide
          text-neutral-500 select-none"
      >
        <span className="mr-1 inline-block transition-transform group-open:rotate-90">›</span>
        Past projects
      </summary>
      {sorted.length === 0 ? (
        <p className="text-sm text-neutral-500">No projects yet.</p>
      ) : (
        <ul className="space-y-1">
          {sorted.map((p) => (
            <li key={p.name}>
              <button
                type="button"
                onClick={() => onSelect(p.name)}
                aria-current={p.name === selected || undefined}
                className={`w-full rounded-lg px-3 py-2 text-left text-sm hover:bg-neutral-100
                  dark:hover:bg-neutral-800 ${
                    p.name === selected ? 'bg-neutral-100 dark:bg-neutral-800' : ''
                  }`}
              >
                <span className="block truncate font-medium">{p.topic}</span>
                <span className="block text-xs text-neutral-500">
                  {formatDate(p.created_at)} · {p.days}d · {p.providers.length} providers
                  {!p.has_report && ' · no report'}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </details>
  )
}
