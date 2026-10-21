import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AddToBrain, BrainList } from './Brains'

const brains = [{ name: 'team', path: '/b/team', notes: 12, runs: 3 }]

function mockFetch(status = 200) {
  const fetchMock = vi.fn(() => Promise.resolve(new Response('{}', { status })))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('BrainList', () => {
  it('lists brains with counts and creates a new one', async () => {
    const fetchMock = mockFetch()
    const onSelect = vi.fn()
    const onChanged = vi.fn()
    render(<BrainList brains={brains} selected={null} onSelect={onSelect} onChanged={onChanged} />)

    fireEvent.click(screen.getByRole('button', { name: /team/ }))
    expect(onSelect).toHaveBeenCalledWith('team')
    expect(screen.getByText('3 runs · 12 notes')).toBeTruthy()

    const create = screen.getByRole('button', { name: 'Create' })
    fireEvent.change(screen.getByLabelText('New brain name'), { target: { value: 'Bad!' } })
    expect(create).toHaveProperty('disabled', true)
    fireEvent.change(screen.getByLabelText('New brain name'), { target: { value: 'notes-2' } })
    fireEvent.click(create)

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/brains',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ name: 'notes-2' }),
      }),
    )
  })
})

describe('AddToBrain', () => {
  it('ingests the project into the named brain', async () => {
    const fetchMock = mockFetch()
    const onAdded = vi.fn()
    render(<AddToBrain project="run-1" brains={brains} onAdded={onAdded} />)

    fireEvent.change(screen.getByLabelText('Add to brain'), { target: { value: 'team' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(await screen.findByText('Added to team.')).toBeTruthy()
    expect(onAdded).toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/brains/team/ingest',
      expect.objectContaining({
        body: JSON.stringify({ projects: ['run-1'] }),
      }),
    )
  })

  it('reports a failed ingest', async () => {
    mockFetch(409)
    render(<AddToBrain project="run-1" brains={brains} onAdded={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Add to brain'), { target: { value: 'team' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByText(/Could not add/)).toBeTruthy()
  })
})
