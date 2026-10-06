import type { AlertDto, CollisionDto, EventDto, ParticipantDto } from '../dto'
import type { IdentityStatus, PlayerRef } from '../../types'

const CONFIRMED_IDENTITY_CONFIDENCE = 0.75

function unknown(status: IdentityStatus = 'UNKNOWN', trackId: number | null = null, identityConfidence: number | null = null): PlayerRef {
  return { trackId, playerId: null, name: 'Player', jersey: null, team: null, identityStatus: status, identityConfidence }
}

function actualPlayerName(name: string | null | undefined): string | null {
  const normalized = name?.trim().replace(/\s+/g, ' ')
  if (!normalized || /^(?:unknown(?: player)?|unidentified(?: player)?|player|player\s*#\s*\d+)$/i.test(normalized)) {
    return null
  }
  return normalized
}

export function playerLabel(name: string | null | undefined, reliable: boolean): string {
  return reliable ? actualPlayerName(name) ?? 'Player' : 'Player'
}

export function playerIdentityLabel(player: PlayerRef): string {
  if (player.playerId === null || player.name === 'Player') return 'Player'
  return [
    player.name,
    player.jersey === null ? null : `#${player.jersey}`,
    player.team,
  ].filter((value): value is string => value !== null).join(' · ')
}

export function hasReliablePlayerIdentity(
  playerId: number | null,
  identifiedBy: string | null,
  confidence: number | null,
  confirmed = true,
): boolean {
  return playerId !== null && confirmed
    && (identifiedBy === 'manual' || (confidence !== null && confidence >= CONFIRMED_IDENTITY_CONFIDENCE))
}

export function safeAlertText(text: string): string {
  return text
    .replace(/\b(?:Unknown\s+Player|Unidentified\s+Player|Unidentified|Unknown|UNKNOWN)\s*(?:\(\s*(?:track\s*)?#?\d+\s*\))?/gi, 'Player')
    .replace(/\bPlayer\s*(?:#\s*\d+|\(\s*(?:track\s*)?#?\d+\s*\))/gi, 'Player')
}

export function eventPlayer(event: EventDto): PlayerRef {
  if (event.player_id === null) {
    const status: IdentityStatus = event.id_confidence !== null && event.id_confidence >= 0.4 && event.id_confidence < 0.75
      ? 'UNCERTAIN' : 'UNKNOWN'
    return unknown(status, event.track_id, event.id_confidence)
  }
  const reliable = hasReliablePlayerIdentity(event.player_id, event.identified_by, event.id_confidence, event.identified)
  const name = playerLabel(event.player_name, reliable)
  return {
    trackId: event.track_id, playerId: event.player_id, name,
    jersey: reliable ? event.jersey_number : null, team: reliable ? event.team_name : null,
    identityStatus: name !== 'Player' ? 'CONFIRMED' : 'UNKNOWN', identityConfidence: event.id_confidence,
  }
}

export function collisionParticipantPlayer(participant: ParticipantDto): PlayerRef {
  if (participant.player_id === null) {
    const status: IdentityStatus = participant.id_confidence !== null && participant.id_confidence >= 0.4 && participant.id_confidence < 0.75
      ? 'UNCERTAIN' : 'UNKNOWN'
    return unknown(status, participant.track_id, participant.id_confidence)
  }
  const hasMatchingPlayer = participant.player?.id === participant.player_id
  const reliable = hasMatchingPlayer && hasReliablePlayerIdentity(
    participant.player_id, participant.identified_by, participant.id_confidence,
  )
  const name = playerLabel(participant.player?.name, reliable)
  return {
    trackId: participant.track_id, playerId: participant.player_id,
    name, jersey: reliable ? participant.player?.jersey_number ?? null : null,
    team: reliable ? participant.player?.team_name ?? null : null,
    identityStatus: name !== 'Player' ? 'CONFIRMED' : 'UNKNOWN', identityConfidence: participant.id_confidence,
  }
}

export function collisionParticipants(collision: CollisionDto): [ParticipantDto, ParticipantDto] | null {
  const ordered = [...collision.participants].sort((a, b) => a.slot - b.slot)
  if (ordered.length !== 2 || ordered[0]?.slot !== 1 || ordered[1]?.slot !== 2) return null
  return [ordered[0], ordered[1]]
}

export function alertPlayerLabel(alert: AlertDto, event?: EventDto, collision?: CollisionDto): string {
  if (alert.injury_event_id !== null) {
    return event?.id === alert.injury_event_id ? eventPlayer(event).name : 'Player'
  }
  if (alert.collision_event_id !== null) {
    if (collision?.id !== alert.collision_event_id) return 'Player'
    const participants = collisionParticipants(collision)
    if (!participants) return 'Player'
    const names = participants.map(participant => collisionParticipantPlayer(participant).name)
    return names.some(name => name !== 'Player') ? names.join(' · ') : 'Player'
  }
  return 'Player'
}
