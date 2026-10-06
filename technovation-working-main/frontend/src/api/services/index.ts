import { api, isConnected } from '../client'
import { EP } from '../endpoints'
import type {
  AlertDto, AlertListParams, AlertStatusBody, CollisionAssignBody, CollisionDto, CollisionListParams,
  EventCreateResult, EventDto, EventListParams, HealthDto, LoginRequest, MatchCreateBody, MatchDto,
  MatchUpdateBody, MedicalAssessmentDto, MeDto, PlayerBriefDto, PlayerCreateBody, PlayerDto, PlayerStatsDto, PlayerUpdateBody,
  MessageDto, PasswordChangeBody, PlayerTrackDto, TeamCreateBody, TeamDto, TeamSummaryDto, TeamUpdateBody, TokenResponse, UserCreateBody, UserUpdateBody, VideoDto,
} from '../dto'

function requireApi() { if (!isConnected) throw new Error('Backend unavailable. Set VITE_API_BASE_URL to enable live data.') }
async function fetchAllPages<T>(
  fetchPage: (offset: number, limit: number) => Promise<T[]>,
  pageSize: number,
): Promise<T[]> {
  const rows: T[] = []
  for (let offset = 0; ; ) {
    const page = await fetchPage(offset, pageSize)
    rows.push(...page)
    if (page.length < pageSize) return rows
    offset += page.length
  }
}

export const session = {
  async login(body: LoginRequest) { requireApi(); return (await api.post<TokenResponse>(EP.login, body)).data },
  async me() { requireApi(); return (await api.get<MeDto>(EP.me)).data },
  async changePassword(body: PasswordChangeBody) {
    requireApi()
    return (await api.post<MessageDto>(EP.changePassword, body)).data
  },
}

export const teams = {
  async list(params?: { include_inactive?: boolean }) { requireApi(); return (await api.get<TeamSummaryDto[]>(EP.teams, { params })).data },
  async get(id: number) { requireApi(); return (await api.get<TeamDto>(`${EP.teams}/${id}`)).data },
  async players(id: number, params?: { include_inactive?: boolean }) { requireApi(); return (await api.get<PlayerBriefDto[]>(`${EP.teams}/${id}/players`, { params })).data },
  async create(body: TeamCreateBody) { requireApi(); return (await api.post<TeamDto>(EP.teams, body)).data },
  async update(id: number, body: TeamUpdateBody) { requireApi(); return (await api.patch<TeamDto>(`${EP.teams}/${id}`, body)).data },
}

export const players = {
  async list(params?: { include_inactive?: boolean }) { requireApi(); return (await api.get<PlayerBriefDto[]>(EP.players, { params })).data },
  async get(id: number) { requireApi(); return (await api.get<PlayerDto>(`${EP.players}/${id}`)).data },
  async stats(id: number) { requireApi(); return (await api.get<PlayerStatsDto>(`${EP.players}/${id}/stats`)).data },
  async create(body: PlayerCreateBody) { requireApi(); return (await api.post<PlayerDto>(EP.players, body)).data },
  async update(id: number, body: PlayerUpdateBody) { requireApi(); return (await api.patch<PlayerDto>(`${EP.players}/${id}`, body)).data },
  async deactivate(id: number) { requireApi(); return (await api.delete(`${EP.players}/${id}`)).data as { message: string } },
}

export const matches = {
  async list(params?: { status?: string }) {
    requireApi()
    const rows = (await api.get<MatchDto[]>(EP.matches, { params })).data
    return rows.map(match => ({
      ...match,
      display_name: match.name?.trim()
        || (match.date ? `Match on ${new Date(match.date).toLocaleDateString()}` : 'Match'),
    }))
  },
  async get(id: number) { requireApi(); return (await api.get<MatchDto>(`${EP.matches}/${id}`)).data },
  async create(body: MatchCreateBody) { requireApi(); return (await api.post<{ message: string; match_session_id: number; match: MatchDto }>(EP.matches, body)).data },
  async update(id: number, body: MatchUpdateBody) { requireApi(); return (await api.patch<MatchDto>(`${EP.matches}/${id}`, body)).data },
  async start(id: number) { requireApi(); return (await api.post<MatchDto>(`${EP.matches}/${id}/start`)).data },
  async end(id: number) { requireApi(); return (await api.post<MatchDto>(`${EP.matches}/${id}/end`)).data },
}

export const events = {
  async list(params?: EventListParams) { requireApi(); return (await api.get<EventDto[]>(EP.events, { params })).data },
  async listAll(params?: Omit<EventListParams, 'limit' | 'offset'>) {
    return fetchAllPages((offset, limit) => events.list({ ...params, limit, offset }), 5000)
  },
  async get(id: number) { requireApi(); return (await api.get<EventDto>(`${EP.events}/${id}`)).data },
  async resolve(id: number) { requireApi(); return (await api.patch<{ message: string }>(`${EP.events}/${id}/resolve`)).data },
  async assign(id: number, player_id: number) { requireApi(); return (await api.patch<{ message: string; event: EventDto }>(`${EP.events}/${id}/assign`, { player_id })).data },
}

export const medicalAssessments = {
  async forEvent(id: number) {
    requireApi()
    return (await api.get<MedicalAssessmentDto>(`${EP.events}/${id}/medical-assessment`)).data
  },
  async forCollision(id: number) {
    requireApi()
    return (await api.get<MedicalAssessmentDto>(`${EP.collisions}/${id}/medical-assessment`)).data
  },
}

export const collisions = {
  async list(params?: CollisionListParams) { requireApi(); return (await api.get<CollisionDto[]>(EP.collisions, { params })).data },
  async listAll(params?: Omit<CollisionListParams, 'limit' | 'offset'>) {
    return fetchAllPages((offset, limit) => collisions.list({ ...params, limit, offset }), 2000)
  },
  async get(id: number) { requireApi(); return (await api.get<CollisionDto>(`${EP.collisions}/${id}`)).data },
  async update(id: number, body: { risk?: string | null; risk_score?: number | null; confidence?: number | null; clip_path?: string | null; note?: string | null; resolved?: boolean | null }) {
    requireApi(); return (await api.patch<CollisionDto>(`${EP.collisions}/${id}`, body)).data
  },
  async assign(id: number, slot: number, body: CollisionAssignBody) {
    requireApi(); return (await api.patch<CollisionDto>(`${EP.collisions}/${id}/assign`, body, { params: { slot } })).data
  },
}

export const alerts = {
  async list(params?: AlertListParams) { requireApi(); return (await api.get<AlertDto[]>(EP.alerts, { params })).data },
  async listAll(params?: Omit<AlertListParams, 'limit' | 'offset'>) {
    return fetchAllPages((offset, limit) => alerts.list({ ...params, limit, offset }), 500)
  },
  async markRead(id: number) { requireApi(); return (await api.patch<AlertDto>(`${EP.alerts}/${id}/read`, {})).data },
  async updateStatus(id: number, body: AlertStatusBody) { requireApi(); return (await api.patch<AlertDto>(`${EP.alerts}/${id}/status`, body)).data },
}

export const users = {
  async list() { requireApi(); return (await api.get<MeDto[]>(EP.users)).data },
  async register(body: UserCreateBody) { requireApi(); return (await api.post<MeDto>(EP.register, body)).data },
  async update(id: number, body: UserUpdateBody) { requireApi(); return (await api.patch<MeDto>(`${EP.users}/${id}`, body)).data },
}

export const videos = {
  async list(params?: { match_id?: number; status?: string; limit?: number; offset?: number }) { requireApi(); return (await api.get<VideoDto[]>(EP.videos, { params })).data },
  async listAll(params?: { match_id?: number; status?: string }) {
    return fetchAllPages((offset, limit) => videos.list({ ...params, limit, offset }), 1000)
  },
  async get(id: number) { requireApi(); return (await api.get<VideoDto>(`${EP.videos}/${id}`)).data },
  async update(id: number, body: Record<string, unknown>) { requireApi(); return (await api.patch<VideoDto>(`${EP.videos}/${id}`, body)).data },
  async upload(matchSessionId: number, file: File, onProgress: (percent: number) => void) {
    requireApi()
    const form = new FormData()
    form.append('match_session_id', String(matchSessionId))
    form.append('file', file)
    return (await api.post<VideoDto>(EP.videoUpload, form, {
      timeout: 0,
      onUploadProgress: (event) => {
        if (event.total) onProgress(Math.round((event.loaded / event.total) * 100))
      },
    })).data
  },
  // The backend accepts metadata JSON only; this is not a file upload method.
  async createMetadata(body: Record<string, unknown>) { requireApi(); return (await api.post<VideoDto>(EP.videos, body)).data },
}

export const tracks = {
  async listForMatch(matchId: number) {
    requireApi()
    return (await api.get<PlayerTrackDto[]>(`/matches/${matchId}/tracks`)).data
  },
}

export const system = { async health() { requireApi(); return (await api.get<HealthDto>(EP.health)).data } }

// Compatibility for the existing pages while they use the typed resource services.
export type ListResource = 'teams' | 'players' | 'matches' | 'events' | 'collisions' | 'alerts' | 'users'
export async function list(resource: ListResource): Promise<unknown[]> {
  switch (resource) {
    case 'teams': return teams.list()
    case 'players': return players.list()
    case 'matches': return matches.list()
    case 'events': return events.list({ limit: 100, offset: 0 })
    case 'collisions': return collisions.list({ limit: 100, offset: 0 })
    case 'alerts': return alerts.list({ limit: 100, offset: 0 })
    case 'users': return users.list()
  }
}

export type { EventCreateResult }
