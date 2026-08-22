-- Remove the legacy pgcrypto key functions. They are unreachable by design now,
-- and one of them was a live foot-gun.
--
-- BACKGROUND
-- ----------
-- Two encryption mechanisms have existed for public.binance_keys:
--
--   LEGACY  save_binance_keys() / decrypt_binance_keys_for()
--           pgp_sym_encrypt/pgp_sym_decrypt, passphrase from the
--           `app.binance_secret` Postgres GUC.
--
--   CURRENT src/lib/binance.functions.ts -> src/lib/crypto.server.ts
--           AES-256-GCM, key derived from BINANCE_KEY_ENCRYPTION_SECRET,
--           stored as [12B iv][16B tag][ciphertext].
--
-- The application uses the CURRENT mechanism exclusively: saveBinanceKeys()
-- encrypts in Node and upserts the row with the service-role client. The legacy
-- functions were left behind and are not called from anywhere in the codebase.
--
-- WHY THEY MUST GO, NOT JUST SIT THERE
-- ------------------------------------
-- The two formats are mutually unreadable, and save_binance_keys() was granted
-- to `authenticated` — meaning any signed-in user could call it over PostgREST
-- and overwrite their own row with a pgp_sym_encrypt blob. Nothing would error.
-- The website would still show a correct api_key_last4, so the account would
-- look connected, while the executor's credentials endpoint could no longer
-- decrypt the row. The user's live trading would simply stop, reported as
-- `user_binance_keys_undecryptable`, with no obvious cause.
--
-- decrypt_binance_keys_for() is the mirror image: service-role-only, so not
-- reachable by a user, but it cannot read any row the current app has written.
-- Calling it would return a pgp_sym_decrypt error rather than a credential.
-- Leaving a plausible-looking "decrypt the user's keys" function in the schema
-- invites exactly the wrong implementation of the very thing this migration
-- accompanies.
--
-- Dropping both leaves one write path and one read path for key material, which
-- is the property the executor's fail-closed behaviour depends on.
--
-- SAFETY
-- ------
-- No table, column, row, grant or RLS policy is touched. binance_keys itself
-- and every stored blob are left exactly as they are. This is reversible by
-- re-running the original migration's function definitions, though the correct
-- recovery for an unreadable row is for the user to re-enter their keys on
-- /app/connect, which rewrites it in the current format.

BEGIN;

DROP FUNCTION IF EXISTS public.save_binance_keys(text, text, text);
DROP FUNCTION IF EXISTS public.decrypt_binance_keys_for(uuid);

COMMIT;

-- Record the surviving contract on the table itself, so the next person to read
-- the schema learns it there rather than from a migration file.
COMMENT ON TABLE public.binance_keys IS
  'Per-user Binance API credentials, encrypted with AES-256-GCM by the app '
  '(src/lib/crypto.server.ts, key from BINANCE_KEY_ENCRYPTION_SECRET). Written '
  'only by saveBinanceKeys(); decrypted only by the server, and served only to '
  'the executor via /api/public/engine/credentials behind ENGINE_CREDENTIALS_TOKEN. '
  'The browser never receives more than api_key_last4. Do not add a pgcrypto '
  'path back: the two formats are mutually unreadable.';
