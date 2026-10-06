-- Reverse 002_player_accounts.sql only before player accounts have been provisioned.
-- The guard prevents silently discarding any user-to-player assignments.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '0';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.users WHERE player_id IS NOT NULL) THEN
        RAISE EXCEPTION 'Cannot reverse player-account migration while user/player links exist';
    END IF;
END
$$;

ALTER TABLE public.users DROP CONSTRAINT IF EXISTS ck_users_player_requires_profile;
ALTER TABLE public.users DROP CONSTRAINT IF EXISTS fk_users_player_id_players;
DROP INDEX IF EXISTS public.ix_users_player_id;
DROP INDEX IF EXISTS public.uq_users_username_lower;
DROP INDEX IF EXISTS public.uq_users_email_lower;
ALTER TABLE public.users DROP COLUMN IF EXISTS player_id;
COMMIT;
