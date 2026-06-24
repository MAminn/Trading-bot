// Server functions for managing the user's Binance API keys.
// Secrets are encrypted server-side with AES-256-GCM. The plaintext secret
// is never returned to the browser — only metadata + last4 of the public key.
import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import { z } from "zod";

const KeyInput = z.object({
  apiKey: z.string().trim().min(16).max(256).regex(/^[A-Za-z0-9]+$/),
  apiSecret: z.string().trim().min(16).max(256).regex(/^[A-Za-z0-9]+$/),
  permissionsNote: z.string().trim().max(500).optional(),
});

export const saveBinanceKeys = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d: unknown) => KeyInput.parse(d))
  .handler(async ({ data, context }) => {
    const { encryptString } = await import("./crypto.server");
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const apiKeyEnc = encryptString(data.apiKey);
    const apiSecretEnc = encryptString(data.apiSecret);
    const { error } = await supabaseAdmin
      .from("binance_keys")
      .upsert({
        user_id: context.userId,
        api_key_encrypted: `\\x${apiKeyEnc.toString("hex")}`,
        api_secret_encrypted: `\\x${apiSecretEnc.toString("hex")}`,
        api_key_last4: data.apiKey.slice(-4),
        permissions_note: data.permissionsNote ?? null,
        updated_at: new Date().toISOString(),
      } as never, { onConflict: "user_id" });
    if (error) throw new Error(error.message);
    return { ok: true, last4: data.apiKey.slice(-4) };
  });

export const getBinanceKeyInfo = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { data, error } = await context.supabase.rpc("get_my_binance_key_info");
    if (error) throw new Error(error.message);
    const row = (data ?? [])[0];
    return row ? (row as { api_key_last4: string; permissions_note: string | null; created_at: string; updated_at: string }) : null;
  });

export const deleteBinanceKeys = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const { error } = await supabaseAdmin
      .from("binance_keys")
      .delete()
      .eq("user_id", context.userId);
    if (error) throw new Error(error.message);
    return { ok: true };
  });
