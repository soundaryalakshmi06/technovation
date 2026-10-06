# AthleteGuard Frontend
React + Vite + TypeScript + Tailwind v4 + React Router + TanStack Query + react-hook-form/zod.

The frontend uses the FastAPI backend for authentication and application data. Set
`VITE_API_BASE_URL` to the backend origin (for local development,
`http://127.0.0.1:8000`) in the local Vite environment before starting the UI.

    npm install
    npm run dev -- --host 127.0.0.1
    npm run build

The login form and role-specific pages use the real API; UI preview is disabled by
default and is only available in development when `VITE_ENABLE_DEV_PREVIEW=true`.
Do not use preview mode for account, authorization, or end-to-end acceptance tests.
