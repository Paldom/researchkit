import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const providers = ['openai', 'gemini', 'grok', 'perplexity', 'tavily', 'claude', 'github', 'glm']

const config = {
  active_preset: 'default',
  presets: ['default', 'optimal'],
  providers,
  default_providers: providers,
  connectors: [],
  default_sites: [],
}

const projects = [
  {
    name: 'proj_a',
    topic: 'agents everywhere',
    days: 7,
    providers: ['openai'],
    created_at: '2026-07-01T10:00:00',
    has_report: true,
  },
]

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/config')) return Response.json(config)
      if (url.endsWith('/api/projects')) return Response.json(projects)
      if (url.endsWith('/report')) return new Response('# Report Title')
      if (url.endsWith('/prompt')) return new Response('ARTICLE PROMPT TEXT')
      if (url.endsWith('/result')) return Response.json({ ok: true })
      if (url.includes('/links')) return Response.json({})
      if (url.endsWith('/log')) return new Response('log line')
      return new Response('not found', { status: 404 })
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('toggles the projects sidebar and remembers it in localStorage', async () => {
    render(<App />)
    expect(await screen.findByText('agents everywhere')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Toggle projects sidebar'))
    expect(screen.queryByText('Past projects')).toBeNull()
    expect(localStorage.getItem('researchkit.sidebar')).toBe('closed')

    fireEvent.click(screen.getByLabelText('Toggle projects sidebar'))
    expect(screen.getByText('Past projects')).toBeTruthy()
    expect(localStorage.getItem('researchkit.sidebar')).toBe('open')
  })

  it('opens a project and renders tab content fetched per tab', async () => {
    render(<App />)
    fireEvent.click(await screen.findByText('agents everywhere'))

    // Report tab loads by default; markdown h1 gets rendered
    expect(await screen.findByText('Report Title')).toBeTruthy()

    fireEvent.click(screen.getByRole('tab', { name: 'Prompt' }))
    expect(await screen.findByText('ARTICLE PROMPT TEXT')).toBeTruthy()
  })
})
