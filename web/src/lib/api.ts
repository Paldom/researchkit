export interface HealthResponse {
  status: string
  version: string
}

export interface ConfigResponse {
  active_preset: string
  presets: string[]
  providers: string[]
  default_providers: string[]
}

export interface UserFile {
  name: string
  content: string
}

export interface ResearchRequest {
  topic: string
  days: number
  providers: string[]
  preset: string | null
  sources: string[]
  include_raw: boolean
  site_research: boolean
  boost: boolean
  user_files: UserFile[]
  user_urls: string[]
  user_texts: string[]
}

export interface ResearchStarted {
  run_id: string
}

export interface Project {
  name: string
  topic: string
  days: number
  providers: string[]
  created_at: string
  has_report: boolean
}

export interface LinkSummary {
  total_occurrences: number
  unique_urls: number
  duplicate_occurrences: number
  duplicate_rate: number
  unique_domains: number
}

export interface LinkStats {
  summary: LinkSummary
  counts_by_provider: Record<string, number>
  top_domains: [string, number][]
  top_duplicates: Record<string, unknown>[]
}

export interface LinksResponse {
  citations?: LinkStats
  site_research?: LinkStats
}

export type LinksMode = 'strict' | 'loose'

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) {
    throw new ApiError(`${init?.method ?? 'GET'} ${path} failed with ${res.status}`, res.status)
  }
  return res.json() as Promise<T>
}

export function getHealth(): Promise<HealthResponse> {
  return requestJson('/api/health')
}

export function getConfig(): Promise<ConfigResponse> {
  return requestJson('/api/config')
}

export function startResearch(body: ResearchRequest): Promise<ResearchStarted> {
  return requestJson('/api/research', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function listProjects(): Promise<Project[]> {
  return requestJson('/api/projects')
}

async function requestText(path: string): Promise<string> {
  const res = await fetch(path)
  if (!res.ok) {
    throw new ApiError(`GET ${path} failed with ${res.status}`, res.status)
  }
  return res.text()
}

export function getReport(name: string): Promise<string> {
  return requestText(`/api/projects/${encodeURIComponent(name)}/report`)
}

export function getPrompt(name: string): Promise<string> {
  return requestText(`/api/projects/${encodeURIComponent(name)}/prompt`)
}

export function getLog(name: string): Promise<string> {
  return requestText(`/api/projects/${encodeURIComponent(name)}/log`)
}

export function getResult(name: string): Promise<unknown> {
  return requestJson(`/api/projects/${encodeURIComponent(name)}/result`)
}

export function getLinks(name: string, mode: LinksMode): Promise<LinksResponse> {
  return requestJson(`/api/projects/${encodeURIComponent(name)}/links?mode=${mode}`)
}

export function improveTopic(topic: string): Promise<{ topic: string }> {
  return requestJson('/api/improve-topic', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ topic }),
  })
}

export function setActivePreset(
  preset: string,
): Promise<{ active_preset: string; presets: string[] }> {
  return requestJson('/api/config/preset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preset }),
  })
}
