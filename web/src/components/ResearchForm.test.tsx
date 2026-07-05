import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ConfigResponse } from '../lib/api'
import ResearchForm from './ResearchForm'

const config: ConfigResponse = {
  active_preset: 'default',
  presets: ['default', 'optimal'],
  providers: ['openai', 'gemini', 'grok', 'perplexity'],
  default_providers: ['openai', 'gemini'],
}

describe('ResearchForm', () => {
  it('pre-checks the default providers from config', () => {
    render(<ResearchForm config={config} running={false} onSubmit={() => undefined} />)
    expect(screen.getByLabelText<HTMLInputElement>('openai').checked).toBe(true)
    expect(screen.getByLabelText<HTMLInputElement>('gemini').checked).toBe(true)
    expect(screen.getByLabelText<HTMLInputElement>('grok').checked).toBe(false)
    expect(screen.getByLabelText<HTMLInputElement>('perplexity').checked).toBe(false)
  })

  it('submits the full research request', () => {
    const onSubmit = vi.fn()
    render(<ResearchForm config={config} running={false} onSubmit={onSubmit} />)

    fireEvent.change(screen.getByLabelText('Topic'), { target: { value: '  AI agents  ' } })
    fireEvent.change(screen.getByLabelText('Days back'), { target: { value: '14' } })
    fireEvent.click(screen.getByLabelText('grok'))
    fireEvent.submit(screen.getByRole('button', { name: 'Start research' }))

    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      topic: 'AI agents',
      days: 14,
      providers: ['openai', 'gemini', 'grok'],
      preset: 'default',
      sources: ['social', 'web'],
    })
  })

  it('disables submit while running and when topic is empty', () => {
    const { rerender } = render(
      <ResearchForm config={config} running={false} onSubmit={() => undefined} />,
    )
    const button = screen.getByRole('button')
    expect(button).toHaveProperty('disabled', true) // empty topic

    fireEvent.change(screen.getByLabelText('Topic'), { target: { value: 'x' } })
    expect(button).toHaveProperty('disabled', false)

    rerender(<ResearchForm config={config} running={true} onSubmit={() => undefined} />)
    expect(button).toHaveProperty('disabled', true)
  })
})
