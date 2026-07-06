import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ConfigResponse } from '../lib/api'
import ResearchForm from './ResearchForm'

const ALL_PROVIDERS = [
  'openai',
  'gemini',
  'grok',
  'perplexity',
  'tavily',
  'claude',
  'github',
  'glm',
]

const config: ConfigResponse = {
  active_preset: 'default',
  presets: ['default', 'optimal'],
  providers: ALL_PROVIDERS,
  default_providers: ALL_PROVIDERS,
}

const noop = () => undefined

describe('ResearchForm', () => {
  it('pre-checks exactly the providers config marks as default', () => {
    const { unmount } = render(<ResearchForm config={config} running={false} onSubmit={noop} />)
    for (const p of ALL_PROVIDERS) {
      expect(screen.getByLabelText<HTMLInputElement>(p).checked).toBe(true)
    }
    unmount()

    render(
      <ResearchForm
        config={{ ...config, default_providers: ['openai'] }}
        running={false}
        onSubmit={noop}
      />,
    )
    expect(screen.getByLabelText<HTMLInputElement>('openai').checked).toBe(true)
    expect(screen.getByLabelText<HTMLInputElement>('gemini').checked).toBe(false)
  })

  it('submits the full research request with custom days and sources', () => {
    const onSubmit = vi.fn()
    render(<ResearchForm config={config} running={false} onSubmit={onSubmit} />)

    fireEvent.change(screen.getByLabelText('Topic'), { target: { value: '  AI agents  ' } })
    fireEvent.change(screen.getByLabelText('Days'), { target: { value: '45' } })
    fireEvent.click(screen.getByLabelText('perplexity')) // uncheck one

    fireEvent.click(screen.getByRole('button', { name: /^Links/ }))
    fireEvent.change(screen.getByLabelText('URLs (one per line)'), {
      target: { value: 'https://a.com\n\n https://b.com ' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Text/ }))
    fireEvent.change(screen.getByLabelText('Free-form context'), {
      target: { value: 'pasted context' },
    })

    fireEvent.submit(screen.getByRole('button', { name: 'Start research' }))

    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      topic: 'AI agents',
      days: 45,
      providers: ['openai', 'gemini', 'grok', 'tavily', 'claude', 'github', 'glm'],
      preset: 'default',
      sources: ['social', 'web'],
      include_raw: true,
      site_research: true,
      boost: false,
      user_files: [],
      user_urls: ['https://a.com', 'https://b.com'],
      user_texts: ['pasted context'],
    })
  })

  it('quick-pick chips set the days input and out-of-range days block submit', () => {
    render(<ResearchForm config={config} running={false} onSubmit={noop} />)
    fireEvent.change(screen.getByLabelText('Topic'), { target: { value: 'x' } })

    fireEvent.click(screen.getByRole('button', { name: '90' }))
    expect(screen.getByLabelText<HTMLInputElement>('Days').value).toBe('90')

    fireEvent.change(screen.getByLabelText('Days'), { target: { value: '500' } })
    expect(screen.getByRole('button', { name: 'Start research' })).toHaveProperty('disabled', true)

    fireEvent.click(screen.getByRole('button', { name: '365' }))
    expect(screen.getByRole('button', { name: 'Start research' })).toHaveProperty('disabled', false)
  })

  it('switches between the custom source tabs', () => {
    render(<ResearchForm config={config} running={false} onSubmit={noop} />)

    expect(screen.getByText(/Drag documents here/)).toBeTruthy() // Files is the default tab

    fireEvent.click(screen.getByRole('button', { name: /^Text/ }))
    expect(screen.getByPlaceholderText('Paste context…')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /^Links/ }))
    expect(screen.getByLabelText('URLs (one per line)')).toBeTruthy()
  })

  it('adds valid files, rejects bad ones, and removes files', async () => {
    render(<ResearchForm config={config} running={false} onSubmit={noop} />)

    const good = new File(['# notes'], 'notes.md', { type: 'text/markdown' })
    const wrongType = new File(['MZ'], 'evil.exe', { type: 'application/octet-stream' })

    fireEvent.change(screen.getByLabelText('Add files'), { target: { files: [good, wrongType] } })

    expect(await screen.findByText(/notes\.md/)).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('evil.exe')
    expect(screen.getByRole('button', { name: /^Files \(1\)/ })).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Remove notes.md'))
    expect(screen.queryByText(/notes\.md/)).toBeNull()
  })

  it('boost disables the whole custom sources section', () => {
    render(<ResearchForm config={config} running={false} onSubmit={noop} />)
    fireEvent.click(screen.getByLabelText('Boost (LLM council)'))

    for (const tab of ['Files', 'Links', 'Text']) {
      expect(screen.getByRole('button', { name: new RegExp(`^${tab}`) })).toHaveProperty(
        'disabled',
        true,
      )
    }
    expect(screen.getByRole('button', { name: 'browse' })).toHaveProperty('disabled', true)
    expect(screen.getByLabelText<HTMLInputElement>('Add files').disabled).toBe(true)
  })

  it('disables submit while running and when topic is empty', () => {
    const { rerender } = render(<ResearchForm config={config} running={false} onSubmit={noop} />)
    const button = screen.getByRole('button', { name: 'Start research' })
    expect(button).toHaveProperty('disabled', true) // empty topic

    fireEvent.change(screen.getByLabelText('Topic'), { target: { value: 'x' } })
    expect(button).toHaveProperty('disabled', false)

    rerender(<ResearchForm config={config} running={true} onSubmit={noop} />)
    expect(screen.getByRole('button', { name: 'Researching…' })).toHaveProperty('disabled', true)
  })
})
