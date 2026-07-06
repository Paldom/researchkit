export interface RunProgress {
  stage: string
  message: string
  provider?: string
  done?: number
  total?: number
}

function asRecord(data: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(data)
    return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/** Parse the payload of an SSE `progress` event. Returns null on malformed data. */
export function parseProgress(data: string): RunProgress | null {
  const v = asRecord(data)
  if (!v || typeof v.stage !== 'string' || typeof v.message !== 'string') return null
  return {
    stage: v.stage,
    message: v.message,
    provider: typeof v.provider === 'string' ? v.provider : undefined,
    done: typeof v.done === 'number' ? v.done : undefined,
    total: typeof v.total === 'number' ? v.total : undefined,
  }
}

/** Parse the payload of an SSE `done` event. Returns the project name or null. */
export function parseDone(data: string): string | null {
  const v = asRecord(data)
  return v && typeof v.project === 'string' ? v.project : null
}

/** Parse the payload of an SSE `error` event. Returns the error message or null. */
export function parseErrorMessage(data: string): string | null {
  const v = asRecord(data)
  return v && typeof v.message === 'string' ? v.message : null
}

export interface RunHandlers {
  onProgress: (event: RunProgress) => void
  onDone: (project: string) => void
  onError: (message: string) => void
}

/**
 * Subscribe to the event stream of a research run.
 * Closes itself on terminal events; returns a function to close it early.
 */
export function subscribeToRun(runId: string, handlers: RunHandlers): () => void {
  const es = new EventSource(`/api/research/${encodeURIComponent(runId)}/events`)

  es.addEventListener('progress', (e) => {
    const progress = parseProgress((e as MessageEvent<string>).data)
    if (progress) handlers.onProgress(progress)
  })

  es.addEventListener('done', (e) => {
    es.close()
    const project = parseDone((e as MessageEvent<string>).data)
    if (project) handlers.onDone(project)
    else handlers.onError('Run finished but the done event was malformed.')
  })

  es.addEventListener('error', (e) => {
    if (e instanceof MessageEvent) {
      // Server-sent `event: error` — the run failed.
      es.close()
      handlers.onError(parseErrorMessage(e.data) ?? 'The research run failed.')
    } else if (es.readyState === EventSource.CLOSED) {
      // Fatal transport error; EventSource gave up.
      handlers.onError('Lost connection to the event stream.')
    }
    // ponytail: otherwise EventSource reconnects on its own — no retry code here.
  })

  return () => es.close()
}
