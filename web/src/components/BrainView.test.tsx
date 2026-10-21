import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { BrainNote } from '../lib/api'
import BrainView, {
  isBrainLink,
  resolveBrainPath,
  stripFrontmatter,
  wikiLinksToMarkdown,
} from './BrainView'

vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  getBrain: vi.fn(),
  getBrainNote: vi.fn(),
}))

import { getBrain, getBrainNote } from '../lib/api'

const notes: BrainNote[] = [
  { path: 'notes/topics/t1.md', slug: 't1', title: 'Topic one', type: 'topic', url: '' },
  {
    path: 'notes/sources/s1.md',
    slug: 's1',
    title: 'Source one',
    type: 'source',
    url: 'https://x.test/a',
  },
]

describe('brain link helpers', () => {
  it('resolves relative links against the file they appear in', () => {
    expect(resolveBrainPath('index.md', 'notes/topics/t1.md')).toBe('notes/topics/t1.md')
    expect(resolveBrainPath('notes/topics/index.md', 't1.md')).toBe('notes/topics/t1.md')
    expect(resolveBrainPath('notes/topics/t1.md', '../sources/s1.md')).toBe('notes/sources/s1.md')
  })

  it('keeps web links out of the brain and rewrites wiki-links', () => {
    expect(isBrainLink('https://x.test/a.md')).toBe(false)
    expect(isBrainLink('notes/topics/t1.md')).toBe(true)
    expect(wikiLinksToMarkdown('see [[s1]] and [[nope]]', notes)).toBe(
      'see [s1](/notes/sources/s1.md) and [[nope]]',
    )
    expect(stripFrontmatter('---\ntitle: "x"\n---\n\nBody')).toBe('Body')
  })
})

describe('BrainView', () => {
  it('navigates index → note → wiki-linked note → index', async () => {
    vi.mocked(getBrain).mockResolvedValue({
      index: '# Brain index\n\n* [Topic one](notes/topics/t1.md)',
      notes,
    })
    vi.mocked(getBrainNote).mockImplementation(async (_name, path) =>
      path === 'notes/topics/t1.md'
        ? '---\ntitle: "Topic one"\n---\n\nTopic body, see [[s1]]'
        : '---\ntitle: "Source one"\n---\n\nSource body',
    )

    render(<BrainView name="proj" />)

    fireEvent.click(await screen.findByRole('link', { name: 'Topic one' }))
    await screen.findByText(/Topic body/)
    expect(getBrainNote).toHaveBeenCalledWith('proj', 'notes/topics/t1.md', false)

    fireEvent.click(screen.getByRole('link', { name: 's1' }))
    await screen.findByText('Source body')
    expect(getBrainNote).toHaveBeenCalledWith('proj', 'notes/sources/s1.md', false)
    expect(screen.getByRole('link', { name: /source/ }).getAttribute('href')).toBe(
      'https://x.test/a',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Index' }))
    await screen.findByText('Brain index')
  })

  it('explains a missing brain instead of failing', async () => {
    const { ApiError } = await import('../lib/api')
    vi.mocked(getBrain).mockRejectedValue(new ApiError('GET failed with 404', 404))
    render(<BrainView name="proj" />)
    await waitFor(() => expect(screen.getByText(/No brain yet/)).toBeTruthy())
  })
})
