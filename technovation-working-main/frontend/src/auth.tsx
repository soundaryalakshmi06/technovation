import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Role } from './types'
import { session } from './api/services'
import { isDevPreviewEnabled, tokenStore } from './api/client'
import type { MeDto } from './api/dto'

export interface User { id?: number; name: string; username?: string; role: Role; teamId?: number | null; playerId?: number | null; dev?: boolean }
interface Ctx {
  user: User | null; loading: boolean; login: (username: string, password: string) => Promise<void>;
  devPreview: (role: Role) => void; logout: () => void
}
const C = createContext<Ctx>(null as unknown as Ctx)
export const useAuth = () => useContext(C)
export const home: Record<Role, string> = { ADMIN: '/admin/dashboard', COACH: '/coach/dashboard', MEDICAL: '/medical/dashboard', PLAYER: '/player/dashboard' }

function roleFromApi(role: string): Role {
  const normalized = role.toUpperCase()
  if (normalized === 'ADMIN' || normalized === 'COACH' || normalized === 'MEDICAL' || normalized === 'PLAYER') return normalized
  throw new Error('The backend returned an unsupported account role.')
}
function toUser(me: MeDto): User {
  return { id: me.id, username: me.username, name: me.full_name?.trim() || me.username, role: roleFromApi(me.role), teamId: me.team_id, playerId: me.player_id }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    const restore = async () => {
      const saved = tokenStore.get()
      if (!saved) { if (active) setLoading(false); return }
      try {
        const me = await session.me()
        if (active) setUser(toUser(me))
      } catch {
        tokenStore.clear()
        if (active) setUser(null)
      } finally { if (active) setLoading(false) }
    }
    const unauthorized = () => { tokenStore.clear(); setUser(null); setLoading(false) }
    window.addEventListener('athleteguard:unauthorized', unauthorized)
    void restore()
    return () => { active = false; window.removeEventListener('athleteguard:unauthorized', unauthorized) }
  }, [])

  const login = async (username: string, password: string) => {
    const token = await session.login({ username, password })
    tokenStore.set(token.access_token)
    try {
      const me = await session.me()
      setUser(toUser(me))
      window.dispatchEvent(new Event('athleteguard:authenticated'))
    } catch (error) {
      tokenStore.clear()
      setUser(null)
      throw error
    }
  }
  const devPreview = (role: Role) => {
    if (isDevPreviewEnabled) setUser({ name: `Preview ${role.toLowerCase()}`, role, dev: true })
  }
  const logout = () => {
    tokenStore.clear()
    setUser(null)
    window.dispatchEvent(new Event('athleteguard:logout'))
  }
  const value = useMemo(() => ({ user, loading, login, devPreview, logout }), [user, loading])
  return <C.Provider value={value}>{children}</C.Provider>
}
