import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ApiError, getBrain, getBrainNote, type BrainNote, type BrainResponse } from '../lib/api'

type Loaded =
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'ready'; brain: BrainResponse }

const INDEX = 'index.md'

/** Resolve a link relative to the file it appears in, inside the brain. */
export function resolveBrainPath(from: string, href: string): string {
  const parts = from.split('/').slice(0, -1)
  for (const seg of href.split('/')) {
    if (seg === '..') parts.pop()
    else if (seg !== '.' && seg !== '') parts.push(seg)
  }
  return parts.join('/')
}

/** Relative markdown links stay inside the brain; everything else is the web. */
export function isBrainLink(href: string): boolean {
  return !/^[a-z][a-z0-9+.-]*:/i.test(href) && !href.startsWith('#') && href.endsWith('.md')
}

/** `[[slug]]` wiki-links become ordinary links to the note with that slug. */
export function wikiLinksToMarkdown(text: string, notes: BrainNote[]): string {
  const bySlug = new Map(notes.map((n) => [n.slug, n.path]))
  return text.replace(/\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]/g, (match, slug: string) => {
    const path = bySlug.get(slug.trim())
    return path ? `[${slug.trim()}](/${path})` : match
  })
}

/** Frontmatter is provenance, not prose: keep it out of the rendered body. */
export function stripFrontmatter(text: string): string {
  if (!text.startsWith('---\n')) return text
  const end = text.indexOf('\n---', 4)
  return end === -1 ? text : text.slice(end + 4).replace(/^\n+/, '')
}

export default function BrainView({ name }: { name: string }) {
  const [loaded, setLoaded] = useState<Loaded>({ state: 'loading' })
  const [path, setPath] = useState(INDEX)
  // Fetched notes remember their path; the index comes straight from the
  // loaded brain. The parent keys this component by project name, so a new
  // project starts from fresh state instead of resetting it in an effect.
  const [fetchedNote, setFetchedNote] = useState<{ path: string; text: string } | null>(null)
  const note =
    loaded.state !== 'ready'
      ? null
      : path === INDEX
        ? loaded.brain.index
        : fetchedNote?.path === path
          ? fetchedNote.text
          : null

  useEffect(() => {
    let cancelled = false
    getBrain(name).then(
      (brain) => {
        if (!cancelled) setLoaded({ state: 'ready', brain })
      },
      (err: unknown) => {
        if (cancelled) return
        setLoaded({
          state: 'error',
          message:
            err instanceof ApiError && err.status === 404
              ? 'No brain yet — it is built from the archived materials after a run.'
              : 'Failed to load. Is the backend up?',
        })
      },
    )
    return () => {
      cancelled = true
    }
  }, [name])

  useEffect(() => {
    if (loaded.state !== 'ready' || path === INDEX) return
    let cancelled = false
    getBrainNote(name, path).then(
      (text) => {
        if (!cancelled) setFetchedNote({ path, text })
      },
      () => {
        if (!cancelled) setFetchedNote({ path, text: '*This note could not be loaded.*' })
      },
    )
    return () => {
      cancelled = true
    }
  }, [name, path, loaded])

  if (loaded.state === 'loading') {
    return <p className="animate-pulse text-sm text-neutral-500">Loading…</p>
  }
  if (loaded.state === 'error') {
    return <p className="text-sm text-neutral-500">{loaded.message}</p>
  }

  const { notes } = loaded.brain
  const current = notes.find((n) => n.path === path)
  const groups = new Map<string, BrainNote[]>()
  for (const n of notes) groups.set(n.type, [...(groups.get(n.type) ?? []), n])

  // Links are root-relative (leading slash) so nested index pages and notes
  // resolve the same way; wiki-links are rewritten to that form too.
  function open(href: string) {
    setPath(href.startsWith('/') ? href.slice(1) : resolveBrainPath(path, href))
  }

  const body = note === null ? null : wikiLinksToMarkdown(stripFrontmatter(note), notes)

  return (
    <section aria-label="Brain" className="grid gap-4 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <nav aria-label="Brain notes" className="max-h-[36rem] overflow-y-auto text-sm">
        <button
          type="button"
          onClick={() => setPath(INDEX)}
          aria-current={path === INDEX || undefined}
          className={`mb-2 w-full rounded-lg px-2 py-1 text-left font-medium hover:bg-neutral-100 dark:hover:bg-neutral-800 ${
            path === INDEX ? 'bg-orange-50 dark:bg-orange-950/40' : ''
          }`}
        >
          Index
        </button>
        {[...groups.entries()].map(([type, group]) => (
          <div key={type} className="mb-3">
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">
              {type}s · {group.length}
            </h3>
            <ul className="space-y-0.5">
              {group.map((n) => (
                <li key={n.path}>
                  <button
                    type="button"
                    onClick={() => setPath(n.path)}
                    aria-current={n.path === path || undefined}
                    title={n.title}
                    className={`w-full truncate rounded-lg px-2 py-1 text-left hover:bg-neutral-100 dark:hover:bg-neutral-800 ${
                      n.path === path ? 'bg-orange-50 dark:bg-orange-950/40' : ''
                    }`}
                  >
                    {n.title}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>

      <div className="min-w-0">
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-neutral-500">
          <span className="font-mono">{path}</span>
          {current?.url && (
            <a
              href={current.url}
              target="_blank"
              rel="noreferrer"
              className="text-orange-600 underline dark:text-orange-400"
            >
              source ↗
            </a>
          )}
        </div>
        {body === null ? (
          <p className="animate-pulse text-sm text-neutral-500">Loading…</p>
        ) : (
          <article className="prose prose-neutral max-w-none dark:prose-invert prose-a:text-orange-600 dark:prose-a:text-orange-400">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({ href = '', children }) =>
                  isBrainLink(href) ? (
                    <a
                      href={href}
                      onClick={(e) => {
                        e.preventDefault()
                        open(href)
                      }}
                    >
                      {children}
                    </a>
                  ) : (
                    <a href={href} target="_blank" rel="noreferrer">
                      {children}
                    </a>
                  ),
              }}
            >
              {body}
            </ReactMarkdown>
          </article>
        )}
      </div>
    </section>
  )
}
