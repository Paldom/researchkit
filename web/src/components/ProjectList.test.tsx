import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Project } from '../lib/api'
import ProjectList from './ProjectList'

const projects: Project[] = [
  {
    name: 'old_project',
    topic: 'older topic',
    days: 7,
    providers: ['openai'],
    created_at: '2026-01-01T10:00:00',
    has_report: true,
  },
  {
    name: 'new_project',
    topic: 'newer topic',
    days: 3,
    providers: ['openai', 'gemini'],
    created_at: '2026-07-01T10:00:00',
    has_report: false,
  },
]

describe('ProjectList', () => {
  it('renders projects newest first', () => {
    render(<ProjectList projects={projects} selected={null} onSelect={() => undefined} />)
    const buttons = screen.getAllByRole('button')
    expect(buttons[0]?.textContent).toContain('newer topic')
    expect(buttons[1]?.textContent).toContain('older topic')
  })

  it('marks projects without a report', () => {
    render(<ProjectList projects={projects} selected={null} onSelect={() => undefined} />)
    // only new_project (3 days) lacks a report
    expect(screen.getByText(/no report/).textContent).toContain('3d')
  })

  it('calls onSelect with the project name on click', () => {
    const onSelect = vi.fn()
    render(<ProjectList projects={projects} selected={null} onSelect={onSelect} />)
    fireEvent.click(screen.getByText('older topic'))
    expect(onSelect).toHaveBeenCalledExactlyOnceWith('old_project')
  })

  it('shows an empty state', () => {
    render(<ProjectList projects={[]} selected={null} onSelect={() => undefined} />)
    expect(screen.getByText('No projects yet.')).toBeTruthy()
  })
})
