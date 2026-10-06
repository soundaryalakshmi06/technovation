import axios, { AxiosError } from 'axios'

const STORAGE_KEY = 'athleteguard.access_token'
let token: string | null = typeof window === 'undefined' ? null : window.localStorage.getItem(STORAGE_KEY)
export const tokenStore = {
  get: () => token,
  set: (value: string) => { token = value; if (typeof window !== 'undefined') window.localStorage.setItem(STORAGE_KEY, value) },
  clear: () => { token = null; if (typeof window !== 'undefined') window.localStorage.removeItem(STORAGE_KEY) },
}

const base = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim()
export const isConnected = Boolean(base)
export const isDevPreviewEnabled = import.meta.env.DEV && import.meta.env.VITE_ENABLE_DEV_PREVIEW === 'true'
export const api = axios.create({ baseURL: base || undefined, timeout: 15000, headers: { Accept: 'application/json' } })

api.interceptors.request.use((config) => {
  const currentToken = tokenStore.get()
  if (currentToken) config.headers.Authorization = `Bearer ${currentToken}`
  return config
})
api.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      tokenStore.clear()
      if (typeof window !== 'undefined') window.dispatchEvent(new Event('athleteguard:unauthorized'))
    }
    return Promise.reject(error)
  },
)

export function normalizeApiError(error: unknown): string {
  if (!axios.isAxiosError(error)) return error instanceof Error ? error.message : 'Something went wrong.'
  if (!error.response) return 'Network unavailable. Check your connection or backend URL.'
  const status = error.response.status
  const detail = (error.response.data as { detail?: unknown } | undefined)?.detail
  const backendMessage = typeof detail === 'string' ? detail : undefined
  if (status === 401) return 'Invalid credentials or your session has expired.'
  if (status === 403) return backendMessage ?? 'You are not authorized to access this resource.'
  if (status === 404) return backendMessage ?? 'The requested resource was not found.'
  if (status === 409) return backendMessage ?? 'This change conflicts with existing data.'
  if (status === 422) return backendMessage ?? 'Some fields are invalid.'
  if (status === 503) return backendMessage ?? 'The backend database is not ready. Please retry shortly.'
  if (status >= 500) return backendMessage ?? 'Something went wrong on the server. Please try again.'
  return backendMessage ?? `Request failed (${status}).`
}

export type ApiError = AxiosError<{ detail?: string | Array<{ msg?: string }> }>
