export type Role='ADMIN'|'MEDICAL'|'COACH'|'PLAYER'
export type IdentityStatus='CONFIRMED'|'UNCERTAIN'|'UNKNOWN'
export type RiskLevel='LOW'|'MEDIUM'|'HIGH'|'CRITICAL'
/** Provisional domain shape (P): replace via api/mappers once backend contract is known. */
export interface PlayerRef{trackId:number|null;playerId:number|null;name:string;jersey:number|null;team:string|null;identityStatus:IdentityStatus;identityConfidence:number|null}
