import type { CollisionDto, EventDto } from './api/dto'

const RISK_PRIORITY: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }
const RISK_LEVELS = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] as const

export interface IncidentSummary {
  total: number
  collisions: number
  clips: number
  byRisk: Record<(typeof RISK_LEVELS)[number], number>
  unrated: number
}

export function timestampValue(value: string | null | undefined): number {
  if (!value) return 0
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function sortRecent<T extends { timestamp: string | null }>(rows: T[]): T[] {
  return [...rows].sort((a, b) => timestampValue(b.timestamp) - timestampValue(a.timestamp))
}

export function summarizeIncidents(events: EventDto[], collisions: CollisionDto[]): IncidentSummary {
  const collisionIds = new Set(collisions.map(collision => collision.id))
  const linkedRisks = new Map<number, string[]>()
  for (const event of events) {
    if (event.collision_event_id !== null && event.risk) {
      const risks = linkedRisks.get(event.collision_event_id) ?? []
      risks.push(event.risk.toUpperCase())
      linkedRisks.set(event.collision_event_id, risks)
    }
  }

  const incidentRisks: (string | null)[] = collisions.map(collision => {
    const risks = [collision.risk?.toUpperCase(), ...(linkedRisks.get(collision.id) ?? [])]
      .filter((risk): risk is string => risk !== undefined && risk !== null)
      .sort((a, b) => (RISK_PRIORITY[a] ?? 4) - (RISK_PRIORITY[b] ?? 4))
    return risks[0] ?? null
  })
  for (const event of events) {
    if (event.collision_event_id === null || !collisionIds.has(event.collision_event_id)) {
      incidentRisks.push(event.risk?.toUpperCase() ?? null)
    }
  }

  const byRisk: IncidentSummary['byRisk'] = { CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 }
  let unrated = 0
  for (const risk of incidentRisks) {
    if (risk === 'CRITICAL') byRisk.CRITICAL += 1
    else if (risk === 'HIGH') byRisk.HIGH += 1
    else if (risk === 'MEDIUM') byRisk.MEDIUM += 1
    else if (risk === 'LOW') byRisk.LOW += 1
    else unrated += 1
  }

  const collisionClips = new Map(collisions.map(collision => [collision.id, collision.clip_url]))
  const clips = new Set<string>()
  for (const event of events) {
    const clip = event.clip_url ?? (event.collision_event_id === null ? null : collisionClips.get(event.collision_event_id) ?? null)
    if (clip) clips.add(clip)
  }
  for (const collision of collisions) {
    if (collision.clip_url) clips.add(collision.clip_url)
  }

  return { total: incidentRisks.length, collisions: collisions.length, clips: clips.size, byRisk, unrated }
}

export function isWithinDateRange(value: string | null, from: string, to: string): boolean {
  if (!from && !to) return true
  const date = value?.slice(0, 10)
  return !!date && (!from || date >= from) && (!to || date <= to)
}
