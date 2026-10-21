export interface HealthResponse {
  status: string
  version: string
}

export interface ConfigResponse {
  active_preset: string
  presets: string[]
  providers: string[]
  default_providers: string[]
  connectors: string[]
  default_sites: string[]
  // Run defaults each preset declares in models.yaml (empty = none).
  preset_providers?: Record<string, string[]>
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
  site_research_sites: string[] | null
  boost: boolean
  user_files: UserFile[]
  user_urls: string[]
  user_texts: string[]
  // A named shared brain to feed and start from; null = none.
  brain?: string | null
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
  // Traceability. Projects created before provenance existed report
  // origin "unknown" and omit the rest, so these are all optional.
  origin?: string
  origin_pid?: number
  preset?: string
  has_materials?: boolean
  has_article?: boolean
  has_skill?: boolean
  has_brain?: boolean
  is_subproject?: boolean
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

export function getArticle(name: string): Promise<string> {
  return requestText(`/api/projects/${encodeURIComponent(name)}/article`)
}

export function getSkill(name: string): Promise<string> {
  return requestText(`/api/projects/${encodeURIComponent(name)}/skill`)
}

export interface BrainNote {
  path: string // brain-relative, e.g. notes/topics/<slug>.md
  slug: string
  title: string
  type: string // topic | source | report | domain | note
  url: string
}

export interface BrainResponse {
  index: string
  notes: BrainNote[]
}

// A brain lives either inside one project or in the shared registry.
const brainBase = (name: string, shared: boolean) =>
  shared
    ? `/api/brains/${encodeURIComponent(name)}`
    : `/api/projects/${encodeURIComponent(name)}/brain`

export function getBrain(name: string, shared = false): Promise<BrainResponse> {
  return requestJson(brainBase(name, shared))
}

export function getBrainNote(name: string, path: string, shared = false): Promise<string> {
  return requestText(`${brainBase(name, shared)}/note?path=${encodeURIComponent(path)}`)
}

export interface SharedBrain {
  name: string
  path: string
  notes: number
  runs: number
}

// Same rule as the server: lowercase letters, digits, '.', '_' or '-'.
export const BRAIN_NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/

export function listBrains(): Promise<SharedBrain[]> {
  return requestJson('/api/brains')
}

export function createBrain(name: string): Promise<SharedBrain> {
  return requestJson('/api/brains', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}

export function addToBrain(brain: string, projects: string[]): Promise<unknown[]> {
  return requestJson(`/api/brains/${encodeURIComponent(brain)}/ingest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ projects }),
  })
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
