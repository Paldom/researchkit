import { useCallback, useEffect, useRef, useState } from 'react'
import ProgressFeed from './components/ProgressFeed'
import ProjectList from './components/ProjectList'
import ReportView from './components/ReportView'
import ResearchForm from './components/ResearchForm'
import {
  ApiError,
  getConfig,
  getReport,
  listProjects,
  startResearch,
  type ConfigResponse,
  type Project,
  type ResearchRequest,
} from './lib/api'
import { subscribeToRun, type RunProgress } from './lib/sse'

export default function App() {
  const [config, setConfig] = useState<ConfigResponse | null>(null)
  const [apiDown, setApiDown] = useState(false)
  const [projects, setProjects] = useState<Project[]>([])
  const [running, setRunning] = useState(false)
  const [events, setEvents] = useState<RunProgress[]>([])
  const [runError, setRunError] = useState<string | null>(null)
  const [report, setReport] = useState<{ name: string; markdown: string } | null>(null)
  const [reportError, setReportError] = useState<string | null>(null)
  const closeStream = useRef<(() => void) | null>(null)

  const loadInitial = useCallback(async () => {
    try {
      const [cfg, projs] = await Promise.all([getConfig(), listProjects()])
      setConfig(cfg)
      setProjects(projs)
      setApiDown(false)
    } catch {
      setApiDown(true)
    }
  }, [])

  useEffect(() => {
    void loadInitial()
    return () => closeStream.current?.()
  }, [loadInitial])

  const openReport = useCallback(async (name: string) => {
    setReport(null)
    setReportError(null)
    try {
      setReport({ name, markdown: await getReport(name) })
    } catch (err) {
      setReportError(
        err instanceof ApiError && err.status === 404
          ? `"${name}" has no report yet — the run may not have completed.`
          : `Failed to load the report for "${name}".`,
      )
    }
  }, [])

  async function startRun(request: ResearchRequest) {
    closeStream.current?.()
    setRunning(true)
    setEvents([])
    setRunError(null)
    setReport(null)
    setReportError(null)
    try {
      const { run_id } = await startResearch(request)
      closeStream.current = subscribeToRun(run_id, {
        onProgress: (event) => setEvents((prev) => [...prev, event]),
        onDone: (project) => {
          setRunning(false)
          void openReport(project)
          listProjects().then(setProjects, () => undefined)
        },
        onError: (message) => {
          setRunning(false)
          setRunError(message)
        },
      })
    } catch {
      setRunning(false)
      setRunError('Failed to start the research run. Is the backend up?')
    }
  }

  const card =
    'rounded-xl border border-neutral-200 bg-white p-5 shadow-sm ' +
    'dark:border-neutral-800 dark:bg-neutral-900/50'

  return (
    <div className="min-h-screen bg-neutral-50 text-neutral-900 dark:bg-neutral-950 dark:text-neutral-100">
      <header className="border-b border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900/50">
        <div className="mx-auto flex max-w-6xl items-baseline justify-between px-4 py-4">
          <div className="flex items-baseline gap-3">
            <span className="text-xl font-bold tracking-tight">
              research<span className="text-indigo-600 dark:text-indigo-400">kit</span>
            </span>
            <span className="hidden text-sm text-neutral-500 sm:inline">
              define a topic, research everywhere
            </span>
          </div>
          <a
            href="https://github.com/Paldom/researchkit"
            target="_blank"
            rel="noreferrer"
            className="text-sm text-neutral-500 hover:text-neutral-900 dark:hover:text-neutral-100"
          >
            GitHub
          </a>
        </div>
      </header>

      {apiDown && (
        <div
          role="alert"
          className="border-b border-red-200 bg-red-50 px-4 py-3 text-center text-sm
            text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
        >
          Cannot reach the researchkit API.{' '}
          <button
            type="button"
            onClick={() => void loadInitial()}
            className="font-medium underline"
          >
            Retry
          </button>
        </div>
      )}

      <main className="mx-auto grid max-w-6xl gap-6 px-4 py-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <div className="min-w-0 space-y-6">
          {config && (
            <div className={card}>
              <ResearchForm config={config} running={running} onSubmit={startRun} />
            </div>
          )}

          {(running || (events.length > 0 && !report)) && (
            <div className={card}>
              <ProgressFeed events={events} running={running} />
            </div>
          )}

          {runError && (
            <div
              role="alert"
              className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700
                dark:border-red-900 dark:bg-red-950 dark:text-red-300"
            >
              Run failed: {runError}
            </div>
          )}

          {reportError && (
            <div
              role="alert"
              className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800
                dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300"
            >
              {reportError}
            </div>
          )}

          {report && (
            <div className={card}>
              <ReportView name={report.name} markdown={report.markdown} />
            </div>
          )}
        </div>

        <aside className="min-w-0">
          <ProjectList
            projects={projects}
            selected={report?.name ?? null}
            onSelect={(name) => void openReport(name)}
          />
        </aside>
      </main>
    </div>
  )
}
