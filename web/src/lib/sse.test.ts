import { describe, expect, it } from 'vitest'
import { parseDone, parseErrorMessage, parseProgress } from './sse'

describe('parseProgress', () => {
  it('parses a full progress payload', () => {
    const data = JSON.stringify({
      stage: 'provider',
      message: 'openai finished',
      provider: 'openai',
      done: 2,
      total: 4,
    })
    expect(parseProgress(data)).toEqual({
      stage: 'provider',
      message: 'openai finished',
      provider: 'openai',
      done: 2,
      total: 4,
    })
  })

  it('parses a minimal payload and drops wrongly-typed optionals', () => {
    const data = JSON.stringify({ stage: 'search', message: 'querying', done: 'nope' })
    expect(parseProgress(data)).toEqual({
      stage: 'search',
      message: 'querying',
      provider: undefined,
      done: undefined,
      total: undefined,
    })
  })

  it('returns null for missing required fields', () => {
    expect(parseProgress(JSON.stringify({ stage: 'x' }))).toBeNull()
    expect(parseProgress(JSON.stringify({ message: 'x' }))).toBeNull()
  })

  it('returns null for non-JSON and non-object payloads', () => {
    expect(parseProgress('not json')).toBeNull()
    expect(parseProgress('42')).toBeNull()
    expect(parseProgress('null')).toBeNull()
  })
})

describe('parseDone', () => {
  it('extracts the project name', () => {
    expect(parseDone(JSON.stringify({ project: '20260705_ai_agents' }))).toBe('20260705_ai_agents')
  })

  it('returns null for malformed payloads', () => {
    expect(parseDone('{}')).toBeNull()
    expect(parseDone('boom')).toBeNull()
  })
})

describe('parseErrorMessage', () => {
  it('extracts the message', () => {
    expect(parseErrorMessage(JSON.stringify({ message: 'provider exploded' }))).toBe(
      'provider exploded',
    )
  })

  it('returns null for malformed payloads', () => {
    expect(parseErrorMessage('{}')).toBeNull()
    expect(parseErrorMessage('')).toBeNull()
  })
})
