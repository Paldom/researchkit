import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export const actionButtonClass =
  'rounded-lg border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 ' +
  'dark:border-neutral-700 dark:hover:bg-neutral-800'

export function downloadText(filename: string, text: string, mime: string) {
  const url = URL.createObjectURL(new Blob([text], { type: mime }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false)

  function copy() {
    navigator.clipboard.writeText(text).then(
      () => {
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
      },
      () => setCopied(false),
    )
  }

  return (
    <button type="button" onClick={copy} className={actionButtonClass}>
      {copied ? 'Copied!' : label}
    </button>
  )
}

interface Props {
  name: string
  markdown: string
}

export default function ReportView({ name, markdown }: Props) {
  return (
    <section aria-label="Report">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-500">{name}</h2>
        <div className="flex gap-2">
          <CopyButton text={markdown} label="Copy markdown" />
          <button
            type="button"
            onClick={() => downloadText(`${name}.md`, markdown, 'text/markdown')}
            className={actionButtonClass}
          >
            Download .md
          </button>
        </div>
      </div>
      <article className="prose prose-neutral max-w-none dark:prose-invert prose-a:text-orange-600 dark:prose-a:text-orange-400">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
      </article>
    </section>
  )
}
