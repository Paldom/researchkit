import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Props {
  name: string
  markdown: string
}

export default function ReportView({ name, markdown }: Props) {
  const [copied, setCopied] = useState(false)

  function copy() {
    navigator.clipboard.writeText(markdown).then(
      () => {
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
      },
      () => setCopied(false),
    )
  }

  function download() {
    const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `${name}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  const buttonClass =
    'rounded-lg border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 ' +
    'dark:border-neutral-700 dark:hover:bg-neutral-800'

  return (
    <section aria-label="Report">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-500">{name}</h2>
        <div className="flex gap-2">
          <button type="button" onClick={copy} className={buttonClass}>
            {copied ? 'Copied!' : 'Copy markdown'}
          </button>
          <button type="button" onClick={download} className={buttonClass}>
            Download .md
          </button>
        </div>
      </div>
      <article className="prose prose-neutral max-w-none dark:prose-invert prose-a:text-indigo-600 dark:prose-a:text-indigo-400">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
      </article>
    </section>
  )
}
