import { useCallback, useEffect, useRef, useState } from 'react'
import OutputTabs from './components/OutputTabs'
import ProgressFeed from './components/ProgressFeed'
import ProjectList from './components/ProjectList'
import ResearchForm from './components/ResearchForm'
import {
  getConfig,
  listProjects,
  startResearch,
  type ConfigResponse,
  type Project,
  type ResearchRequest,
} from './lib/api'
import { subscribeToRun, type RunProgress } from './lib/sse'

const SIDEBAR_KEY = 'researchkit.sidebar'

export default function App() {
  const [config, setConfig] = useState<ConfigResponse | null>(null)
  const [apiDown, setApiDown] = useState(false)
  const [projects, setProjects] = useState<Project[]>([])
  const [running, setRunning] = useState(false)
  const [events, setEvents] = useState<RunProgress[]>([])
  const [runError, setRunError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(
    () => localStorage.getItem(SIDEBAR_KEY) !== 'closed',
  )
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

  function toggleSidebar() {
    setSidebarOpen((open) => {
      localStorage.setItem(SIDEBAR_KEY, open ? 'closed' : 'open')
      return !open
    })
  }

  async function startRun(request: ResearchRequest) {
    closeStream.current?.()
    setRunning(true)
    setEvents([])
    setRunError(null)
    setSelected(null)
    try {
      const { run_id } = await startResearch(request)
      closeStream.current = subscribeToRun(run_id, {
        onProgress: (event) => setEvents((prev) => [...prev, event]),
        onDone: (project) => {
          setRunning(false)
          setSelected(project)
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
        <div className="flex items-center justify-between px-4 py-3">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={toggleSidebar}
              aria-label="Toggle projects sidebar"
              aria-expanded={sidebarOpen}
              className="rounded-lg border border-neutral-300 px-2.5 py-1.5 text-sm
                hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
            >
              ☰
            </button>
            <span className="text-xl font-bold tracking-tight">
              research<span className="text-orange-600 dark:text-orange-400">kit</span>
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

      <div className="flex">
        {sidebarOpen && (
          <>
            {/* mobile: overlay drawer with backdrop; desktop: static column */}
            <div
              className="fixed inset-0 z-20 bg-black/40 lg:hidden"
              onClick={toggleSidebar}
              aria-hidden="true"
            />
            <aside
              className="fixed inset-y-0 left-0 z-30 w-72 shrink-0 overflow-y-auto border-r
                border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900
                lg:static lg:z-auto lg:bg-transparent lg:dark:bg-transparent"
            >
              <ProjectList projects={projects} selected={selected} onSelect={setSelected} />
            </aside>
          </>
        )}

        <main className="min-w-0 flex-1 px-4 py-6">
          {/* centered reading frame */}
          <div className="mx-auto max-w-[980px] space-y-6">
            {config && (
              <div className={card}>
                <ResearchForm config={config} running={running} onSubmit={startRun} />
              </div>
            )}

            {(running || (events.length > 0 && !selected)) && (
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

            {selected && (
              <div className={card}>
                <OutputTabs name={selected} />
              </div>
            )}
          </div>
        </main>
      </div>
    </div>
  )
}
