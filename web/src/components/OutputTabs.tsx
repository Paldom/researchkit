import { useEffect, useState } from 'react'
import {
  ApiError,
  getLinks,
  getLog,
  getPrompt,
  getReport,
  getResult,
  type LinkStats,
  type LinksMode,
  type LinksResponse,
} from '../lib/api'
import ReportView, { CopyButton, actionButtonClass, downloadText } from './ReportView'

const TABS = ['Report', 'Prompt', 'Raw JSON', 'Links', 'Log'] as const
type Tab = (typeof TABS)[number]

type Loaded =
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'text'; text: string }
  | { state: 'links'; links: LinksResponse }

const preClass =
  'max-h-[36rem] overflow-auto rounded-lg border border-neutral-200 bg-neutral-50 p-3 ' +
  'font-mono text-xs whitespace-pre-wrap dark:border-neutral-800 dark:bg-neutral-900'

export default function OutputTabs({ name }: { name: string }) {
  const [tab, setTab] = useState<Tab>('Report')
  const [mode, setMode] = useState<LinksMode>('strict')
  const [loaded, setLoaded] = useState<Loaded>({ state: 'loading' })

  useEffect(() => {
    let cancelled = false
    setLoaded({ state: 'loading' })
    const fetchers: Record<Tab, () => Promise<Loaded>> = {
      Report: async () => ({ state: 'text', text: await getReport(name) }),
      Prompt: async () => ({ state: 'text', text: await getPrompt(name) }),
      'Raw JSON': async () => ({
        state: 'text',
        text: JSON.stringify(await getResult(name), null, 2),
      }),
      Links: async () => ({ state: 'links', links: await getLinks(name, mode) }),
      Log: async () => ({ state: 'text', text: await getLog(name) }),
    }
    fetchers[tab]().then(
      (result) => {
        if (!cancelled) setLoaded(result)
      },
      (err: unknown) => {
        if (cancelled) return
        setLoaded({
          state: 'error',
          message:
            err instanceof ApiError && err.status === 404
              ? 'Not available yet — run the project first.'
              : 'Failed to load. Is the backend up?',
        })
      },
    )
    return () => {
      cancelled = true
    }
  }, [name, tab, mode])

  return (
    <section aria-label="Project output">
      <div
        role="tablist"
        className="flex flex-wrap gap-1 border-b border-neutral-200 dark:border-neutral-800"
      >
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={t === tab}
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
              t === tab
                ? 'border-orange-500 text-orange-600 dark:text-orange-400'
                : 'border-transparent text-neutral-500 hover:text-neutral-900 dark:hover:text-neutral-100'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      <div role="tabpanel" className="pt-4">
        {loaded.state === 'loading' && (
          <p className="animate-pulse text-sm text-neutral-500">Loading…</p>
        )}
        {loaded.state === 'error' && <p className="text-sm text-neutral-500">{loaded.message}</p>}

        {loaded.state === 'text' && tab === 'Report' && (
          <ReportView name={name} markdown={loaded.text} />
        )}

        {loaded.state === 'text' && tab === 'Prompt' && (
          <div>
            <div className="mb-2 flex justify-end">
              <CopyButton text={loaded.text} label="Copy prompt" />
            </div>
            <pre className={preClass}>{loaded.text}</pre>
          </div>
        )}

        {loaded.state === 'text' && tab === 'Raw JSON' && (
          <div>
            <div className="mb-2 flex justify-end">
              <button
                type="button"
                onClick={() => downloadText(`${name}.json`, loaded.text, 'application/json')}
                className={actionButtonClass}
              >
                Download .json
              </button>
            </div>
            <pre className={preClass}>{loaded.text}</pre>
          </div>
        )}

        {loaded.state === 'text' && tab === 'Log' && (
          <div>
            <div className="mb-2 flex justify-end">
              <button
                type="button"
                onClick={() => downloadText(`${name}.log`, loaded.text, 'text/plain')}
                className={actionButtonClass}
              >
                Download .log
              </button>
            </div>
            <pre className={preClass}>{loaded.text}</pre>
          </div>
        )}

        {loaded.state === 'links' && (
          <LinksPanel links={loaded.links} mode={mode} onMode={setMode} />
        )}
      </div>
    </section>
  )
}

function LinksPanel({
  links,
  mode,
  onMode,
}: {
  links: LinksResponse
  mode: LinksMode
  onMode: (mode: LinksMode) => void
}) {
  const sections = [
    ['Citations', links.citations],
    ['Site research', links.site_research],
  ] as const

  return (
    <div className="space-y-6">
      <div className="flex gap-1">
        {(['strict', 'loose'] as const).map((m) => (
          <button
            key={m}
            type="button"
            aria-pressed={m === mode}
            onClick={() => onMode(m)}
            className={`rounded-lg px-3 py-1 text-sm font-medium ${
              m === mode
                ? 'bg-orange-600 text-white'
                : 'border border-neutral-300 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'
            }`}
          >
            {m}
          </button>
        ))}
      </div>
      {sections.every(([, stats]) => !stats) && (
        <p className="text-sm text-neutral-500">No link statistics in this project.</p>
      )}
      {sections.map(
        ([title, stats]) => stats && <LinkStatsSection key={title} title={title} stats={stats} />,
      )}
    </div>
  )
}

function LinkStatsSection({ title, stats }: { title: string; stats: LinkStats }) {
  const { summary } = stats
  // ponytail: treat rate <= 1 as a fraction, otherwise as an already-computed percentage.
  const rate = summary.duplicate_rate <= 1 ? summary.duplicate_rate * 100 : summary.duplicate_rate
  const tiles: [string, string | number][] = [
    ['Occurrences', summary.total_occurrences],
    ['Unique URLs', summary.unique_urls],
    ['Duplicates', summary.duplicate_occurrences],
    ['Duplicate rate', `${Math.round(rate)}%`],
    ['Domains', summary.unique_domains],
  ]

  return (
    <section aria-label={title}>
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        {title}
      </h3>
      <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-5">
        {tiles.map(([label, value]) => (
          <div
            key={label}
            className="rounded-lg border border-neutral-200 p-2 text-center dark:border-neutral-800"
          >
            <div className="text-lg font-semibold">{value}</div>
            <div className="text-xs text-neutral-500">{label}</div>
          </div>
        ))}
      </div>
      {stats.top_domains.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-neutral-200 text-left text-xs uppercase tracking-wide text-neutral-500 dark:border-neutral-800">
              <th className="py-1.5 font-medium">Domain</th>
              <th className="py-1.5 text-right font-medium">Count</th>
            </tr>
          </thead>
          <tbody>
            {stats.top_domains.map(([domain, count]) => (
              <tr key={domain} className="border-b border-neutral-100 dark:border-neutral-800/50">
                <td className="py-1.5">{domain}</td>
                <td className="py-1.5 text-right">{count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
