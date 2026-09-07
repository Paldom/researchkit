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
    origin: 'web',
    origin_pid: 4242,
    has_materials: true,
  },
  {
    name: 'new_project',
    topic: 'newer topic',
    days: 3,
    providers: ['openai', 'gemini'],
    created_at: '2026-07-01T10:00:00',
    has_report: false,
    origin: 'explore',
    origin_pid: 5150,
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

  it('filters by origin so a web run can be told from an explore run', () => {
    render(<ProjectList projects={projects} selected={null} onSelect={() => undefined} />)
    fireEvent.change(screen.getByLabelText('Filter by origin'), {
      target: { value: 'explore' },
    })
    expect(screen.queryByText('older topic')).toBeNull()
    expect(screen.getByText('newer topic')).toBeTruthy()
  })

  it('filters by output so runs that produced nothing can be found', () => {
    render(<ProjectList projects={projects} selected={null} onSelect={() => undefined} />)
    fireEvent.change(screen.getByLabelText('Filter by output'), {
      target: { value: 'no-report' },
    })
    expect(screen.getByText('newer topic')).toBeTruthy()
    expect(screen.queryByText('older topic')).toBeNull()
  })

  it('searches topic, folder name and pid', () => {
    render(<ProjectList projects={projects} selected={null} onSelect={() => undefined} />)
    const search = screen.getByLabelText('Filter projects')
    fireEvent.change(search, { target: { value: 'old_project' } })
    expect(screen.getByText('older topic')).toBeTruthy()
    // A pid ties a project back to `researchkit runs` and its OS process.
    fireEvent.change(search, { target: { value: '5150' } })
    expect(screen.getByText('newer topic')).toBeTruthy()
    expect(screen.queryByText('older topic')).toBeNull()
  })

  it('distinguishes no projects from no matches', () => {
    render(<ProjectList projects={projects} selected={null} onSelect={() => undefined} />)
    fireEvent.change(screen.getByLabelText('Filter projects'), {
      target: { value: 'zzz nothing' },
    })
    expect(screen.getByText('No projects match these filters.')).toBeTruthy()
  })

  it('shows an empty state', () => {
    render(<ProjectList projects={[]} selected={null} onSelect={() => undefined} />)
    expect(screen.getByText('No projects yet.')).toBeTruthy()
  })
})
