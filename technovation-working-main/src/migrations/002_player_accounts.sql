-- Link player login accounts to the existing players roster without changing existing rows.
-- Apply as the database/object owner. Safe to run again after a partial deployment.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '0';

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS player_id INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass
          AND conname = 'fk_users_player_id_players'
    ) THEN
        ALTER TABLE public.users
            ADD CONSTRAINT fk_users_player_id_players
            FOREIGN KEY (player_id) REFERENCES public.players(id) ON DELETE SET NULL;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_player_id ON public.users (player_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_lower ON public.users (lower(username));
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON public.users (lower(email))
    WHERE email IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass
          AND conname = 'ck_users_player_requires_profile'
    ) THEN
        ALTER TABLE public.users
            ADD CONSTRAINT ck_users_player_requires_profile
            CHECK (role <> 'player' OR player_id IS NOT NULL) NOT VALID;
    END IF;
END
$$;

ALTER TABLE public.users VALIDATE CONSTRAINT ck_users_player_requires_profile;
COMMIT;
