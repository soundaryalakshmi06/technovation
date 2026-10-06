export type DateTime = string
import type { RiskLevel } from '../../types'

export interface LoginRequest { username: string; password: string }
export interface PasswordChangeBody { current_password: string; new_password: string }
export interface MessageDto { message: string }
export interface TokenResponse { access_token: string; token_type: string; role: string; team_id: number | null; username: string }
export interface MeDto { id: number; username: string; role: string; team_id: number | null; player_id: number | null; full_name: string | null; email: string | null; phone: string | null; active: boolean; last_login_at: DateTime | null; created_at: DateTime | null }

export interface TeamDto { id: number; name: string; short_name: string | null; active: boolean; created_at: DateTime | null }
export interface PlayerStatsDto { id: number; name: string; jersey_number: number; injury_events: number; high_risk_events: number }
export interface TeamSummaryDto { id: number; name: string; coaches: string[]; player_count: number; injury_events: number; high_risk_events: number; players: PlayerStatsDto[] }
export interface PlayerBriefDto { id: number; name: string; jersey_number: number; team_id: number; team_name: string | null }
export interface PlayerDto { id: number; team_id: number; team_name: string | null; name: string; jersey_number: number; position: string | null; active: boolean; reference_images: string[] | null; created_at: DateTime | null }

export interface MatchDto { id: number; name: string | null; video_source: string | null; date: DateTime | null; team_a_id: number | null; team_b_id: number | null; status: string; created_at: DateTime | null; display_name: string }

export interface EventDto {
  id: number; player_id: number | null; player_name: string; jersey_number: number | null; team_name: string | null;
  identified: boolean; identified_at: DateTime | null; identified_by: string | null; id_detail: string | null;
  id_confidence: number | null; track_id: number | null; player_track_id: number | null; video_id: number | null;
  collision_event_id: number | null; video_time_sec: number | null; frame_number: number | null;
  match_session_id: number | null; match_name: string | null; event_type: string; region: string | null;
  side: string | null; risk: RiskLevel | null; risk_score: number | null; confidence: number | null;
  movement: number | null; note: string | null; injury_note: string | null; clip_url: string | null;
  source: string; resolved: boolean | null; resolved_at: DateTime | null; location_x: number | null;
  location_y: number | null; detection_count: number; timestamp: DateTime | null;
}
export interface EventListParams { match_id?: number; player_id?: number; event_type?: string; risk?: string; resolved?: boolean; limit?: number; offset?: number }
export interface EventCreateResult { message: string; event_id: number; duplicate: boolean }
export interface PlayerTrackDto {
  id: number; match_session_id: number; video_id: number | null; track_id: number; player_id: number | null;
  team_id: number | null; identity_status: string; identified_by: string | null; id_detail: string | null;
  id_confidence: number | null; identified_at: DateTime | null; predecessor_id: number | null;
  first_frame: number | null; last_frame: number | null; first_seen_sec: number | null; last_seen_sec: number | null;
  tracking_confidence: number | null; player: PlayerBriefDto | null;
}

export interface ParticipantDto {
  id: number; collision_event_id: number | null; slot: number; track_id: number; player_id: number | null;
  player_track_id: number | null; identified_by: string | null; id_detail: string | null; id_confidence: number | null;
  identified_at: DateTime | null; region: string | null; movement: number | null;
  player: PlayerBriefDto | null;
}
export interface CollisionDto {
  id: number; match_session_id: number | null; video_id: number | null; event_type: string; risk: RiskLevel | null;
  risk_score: number | null; confidence: number | null; iou: number | null; contact_frames: number | null;
  frame_number: number | null; video_time_sec: number | null; location_x: number | null; location_y: number | null;
  location_bbox: number[] | null; region: string | null; note: string | null; source: string; resolved: boolean;
  resolved_at: DateTime | null; detection_count: number; timestamp: DateTime | null;
  participants: ParticipantDto[]; track_ids: number[]; player_ids: number[]; clip_url: string | null;
}
export interface CollisionListParams { match_id?: number; player_id?: number; limit?: number; offset?: number }
export interface CollisionAssignBody { player_id: number; player_track_id?: number | null; identified_by?: string | null; id_detail?: string | null; id_confidence?: number | null }

export interface AlertDto {
  id: number; injury_event_id: number | null; collision_event_id: number | null; recipient_user_id: number | null;
  team_id: number | null; alert_type: string; severity: RiskLevel | null; channel: string; title: string | null;
  message: string; status: string; sent_at: DateTime | null; error_message: string | null; is_read: boolean;
  read_at: DateTime | null; created_at: DateTime | null;
}
export interface MedicalAssessmentDto {
  id: string; injury_event_id: number | null; collision_event_id: number | null; player_id: number | null;
  player_name: string; event_type: string; affected_region: string; risk_level: RiskLevel | null;
  observed_signs: string[]; assessment_summary: string; recommended_action: string;
  requires_medical_attention: boolean; confidence: number | null; evidence_available: boolean;
  evidence_clip_url: string | null; frame_number: number | null; video_time_sec: number | null;
  collision_context: string | null; disclaimer: string; created_at: DateTime;
}
export interface AlertListParams { unread?: boolean; match_id?: number; severity?: string; alert_type?: string; limit?: number; offset?: number }
export interface AlertStatusBody { status: string; error_message?: string | null }
export interface VideoDto { id: number; match_session_id: number; source_type: string; original_filename: string | null; file_path: string | null; content_type: string | null; file_size_bytes: number | null; checksum_sha256: string | null; fps: number | null; width: number | null; height: number | null; total_frames: number | null; duration_sec: number | null; status: string; processed_at: DateTime | null; error_message: string | null; created_at: DateTime | null }

export interface UserCreateBody { username: string; password: string; role: string; team_id?: number | null; player_id?: number | null; full_name?: string | null; email?: string | null; phone?: string | null; active?: boolean }
export interface UserUpdateBody { role?: string | null; team_id?: number | null; player_id?: number | null; full_name?: string | null; email?: string | null; phone?: string | null; active?: boolean | null; password?: string | null }
export interface TeamCreateBody { name: string; short_name?: string | null; active?: boolean }
export interface TeamUpdateBody { name?: string | null; short_name?: string | null; active?: boolean | null }
export interface PlayerCreateBody { team_id: number; name: string; jersey_number: number; position?: string | null; active?: boolean; reference_images?: string[] | null; face_embedding?: number[] | null }
export interface PlayerUpdateBody { team_id?: number | null; name?: string | null; jersey_number?: number | null; position?: string | null; active?: boolean | null; reference_images?: string[] | null; face_embedding?: number[] | null }
export interface MatchCreateBody { name?: string | null; video_source?: string | null; team_a_id?: number | null; team_b_id?: number | null; date?: DateTime | null }
export interface MatchUpdateBody extends Partial<MatchCreateBody> { status?: string | null }
export interface HealthDto { status: string; database: string; version: string; environment: string }

// Reports are computed from existing APIs; medical assessments are generated on demand and are not persisted.
