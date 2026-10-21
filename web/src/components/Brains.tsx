import { useState, type FormEvent } from 'react'
import { BRAIN_NAME, addToBrain, createBrain, type SharedBrain } from '../lib/api'

const inputClass =
  'min-w-0 flex-1 rounded-lg border border-neutral-300 bg-white px-2 py-1 text-sm ' +
  'focus:border-orange-500 focus:outline-none dark:border-neutral-700 dark:bg-neutral-900'
const buttonClass =
  'rounded-lg border border-neutral-300 px-2.5 py-1 text-xs font-medium hover:bg-neutral-100 ' +
  'disabled:cursor-not-allowed disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800'

interface ListProps {
  brains: SharedBrain[]
  selected: string | null
  onSelect: (name: string) => void
  onChanged: () => void
}

/** Shared brains in the sidebar: open one, or create one in advance. */
export function BrainList({ brains, selected, onSelect, onChanged }: ListProps) {
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const valid = BRAIN_NAME.test(name.trim())

  async function create(e: FormEvent) {
    e.preventDefault()
    if (!valid) return
    setError(null)
    try {
      await createBrain(name.trim())
      setName('')
      onChanged()
    } catch {
      setError('Could not create the brain.')
    }
  }

  return (
    <section aria-label="Shared brains" className="mt-6">
      <h2 className="mb-2 text-sm font-semibold">Shared brains</h2>
      <ul className="mb-2 space-y-0.5 text-sm">
        {brains.map((b) => (
          <li key={b.name}>
            <button
              type="button"
              onClick={() => onSelect(b.name)}
              aria-current={b.name === selected || undefined}
              className={`flex w-full justify-between rounded-lg px-2 py-1 text-left hover:bg-neutral-100 dark:hover:bg-neutral-800 ${
                b.name === selected ? 'bg-orange-50 dark:bg-orange-950/40' : ''
              }`}
            >
              <span className="truncate">{b.name}</span>
              <span className="text-xs text-neutral-500">
                {b.runs} runs · {b.notes} notes
              </span>
            </button>
          </li>
        ))}
      </ul>
      <form onSubmit={(e) => void create(e)} className="flex gap-1.5">
        <input
          aria-label="New brain name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="new brain"
          className={inputClass}
        />
        <button type="submit" disabled={!valid} className={buttonClass}>
          Create
        </button>
      </form>
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </section>
  )
}

interface AddProps {
  project: string
  brains: SharedBrain[]
  onAdded: () => void
}

/** Attach a finished project to a shared brain (new name = new brain). */
export function AddToBrain({ project, brains, onAdded }: AddProps) {
  const [brain, setBrain] = useState('')
  const [status, setStatus] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const target = brain.trim()

  async function add(e: FormEvent) {
    e.preventDefault()
    if (!BRAIN_NAME.test(target)) return
    setBusy(true)
    setStatus(null)
    try {
      await addToBrain(target, [project])
      setStatus(`Added to ${target}.`)
      onAdded()
    } catch {
      setStatus('Could not add this project (does it have results?).')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void add(e)} className="mb-4 flex flex-wrap items-center gap-1.5">
      <input
        aria-label="Add to brain"
        list="add-brain-names"
        value={brain}
        onChange={(e) => setBrain(e.target.value)}
        placeholder="add to brain…"
        className={`max-w-56 ${inputClass}`}
      />
      <datalist id="add-brain-names">
        {brains.map((b) => (
          <option key={b.name} value={b.name} />
        ))}
      </datalist>
      <button type="submit" disabled={busy || !BRAIN_NAME.test(target)} className={buttonClass}>
        {busy ? 'Adding…' : 'Add'}
      </button>
      {status && <span className="text-xs text-neutral-500">{status}</span>}
    </form>
  )
}
