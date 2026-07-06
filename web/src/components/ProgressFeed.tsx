import { useEffect, useRef } from 'react'
import type { RunProgress } from '../lib/sse'

interface Props {
  events: RunProgress[]
  running: boolean
}

export default function ProgressFeed({ events, running }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [events])

  return (
    <section aria-label="Run progress">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Progress
      </h2>
      <div
        ref={scrollRef}
        className="max-h-80 space-y-1 overflow-y-auto rounded-lg border border-neutral-200
          bg-neutral-50 p-3 font-mono text-xs dark:border-neutral-800 dark:bg-neutral-900"
      >
        {events.map((e, i) => (
          <div key={i} className="flex items-baseline gap-2">
            <span
              className="shrink-0 rounded bg-orange-100 px-1.5 py-0.5 font-sans font-medium
                text-orange-700 dark:bg-orange-950 dark:text-orange-300"
            >
              {e.stage}
            </span>
            <span className="min-w-0 break-words">
              {e.provider ? `${e.provider}: ` : ''}
              {e.message}
            </span>
            {e.done !== undefined && e.total !== undefined && (
              <span className="ml-auto shrink-0 text-neutral-500">
                {e.done}/{e.total}
              </span>
            )}
          </div>
        ))}
        {running && <div className="animate-pulse text-neutral-500">working…</div>}
      </div>
    </section>
  )
}
