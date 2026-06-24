import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { KeyRound, LogOut, Mail, ShieldAlert, User, Loader2, Check, Upload, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { toast } from "sonner";

export const Route = createFileRoute("/app/settings")({
  head: () => ({ meta: [{ title: "Settings — Helix" }] }),
  component: Settings,
});

const AVATAR_MAX_BYTES = 5 * 1024 * 1024; // 5 MB
const AVATAR_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

async function signedAvatarUrl(path: string): Promise<string | null> {
  const { data, error } = await supabase.storage
    .from("avatars")
    .createSignedUrl(path, 60 * 60);
  if (error) return null;
  return data.signedUrl;
}

type ProfileForm = {
  full_name: string;
  email: string;
  timezone: string;
  plan: string;
};

function Settings() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [initialEmail, setInitialEmail] = useState("");
  const [userId, setUserId] = useState<string | null>(null);
  const [avatarPath, setAvatarPath] = useState<string | null>(null);
  const [avatarUrl, setAvatarUrl] = useState<string | null>(null);
  const [avatarUploading, setAvatarUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [form, setForm] = useState<ProfileForm>({
    full_name: "",
    email: "",
    timezone: "UTC+02:00 · Athens",
    plan: "Pro · Tier 2",
  });

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getUser();
      const u = data.user;
      if (u) {
        const meta = (u.user_metadata ?? {}) as Record<string, string>;
        setUserId(u.id);
        setForm({
          full_name: meta.full_name ?? "",
          email: u.email ?? "",
          timezone: meta.timezone ?? "UTC+02:00 · Athens",
          plan: meta.plan ?? "Pro · Tier 2",
        });
        setInitialEmail(u.email ?? "");
        if (meta.avatar_path) {
          setAvatarPath(meta.avatar_path);
          setAvatarUrl(await signedAvatarUrl(meta.avatar_path));
        }
      }
      setLoading(false);
    })();
  }, []);

  async function handleAvatarChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !userId) return;

    if (!AVATAR_TYPES.includes(file.type)) {
      toast.error("Please use a PNG, JPEG, WEBP, or GIF image.");
      return;
    }
    if (file.size > AVATAR_MAX_BYTES) {
      toast.error("Image must be under 5 MB.");
      return;
    }

    setAvatarUploading(true);
    const toastId = toast.loading("Uploading avatar…");
    try {
      const ext = file.name.split(".").pop()?.toLowerCase() || "png";
      const path = `${userId}/avatar-${Date.now()}.${ext}`;
      const { error: uploadError } = await supabase.storage
        .from("avatars")
        .upload(path, file, { upsert: true, contentType: file.type });
      if (uploadError) throw uploadError;

      const { error: metaError } = await supabase.auth.updateUser({
        data: { avatar_path: path },
      });
      if (metaError) throw metaError;

      // Best-effort cleanup of the previous file
      if (avatarPath && avatarPath !== path) {
        await supabase.storage.from("avatars").remove([avatarPath]);
      }

      setAvatarPath(path);
      setAvatarUrl(await signedAvatarUrl(path));
      toast.success("Avatar uploaded successfully.", { id: toastId });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Avatar upload failed. Please try again.";
      toast.error(msg, { id: toastId });
    } finally {
      setAvatarUploading(false);
    }
  }

  async function handleAvatarRemove() {
    if (!avatarPath) return;
    setAvatarUploading(true);
    const toastId = toast.loading("Removing avatar…");
    try {
      await supabase.storage.from("avatars").remove([avatarPath]);
      const { error } = await supabase.auth.updateUser({ data: { avatar_path: null } });
      if (error) throw error;
      setAvatarPath(null);
      setAvatarUrl(null);
      toast.success("Avatar removed.", { id: toastId });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Could not remove avatar.";
      toast.error(msg, { id: toastId });
    } finally {
      setAvatarUploading(false);
    }
  }


  function update<K extends keyof ProfileForm>(key: K, value: ProfileForm[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;

    // Basic validation
    if (form.full_name.trim().length === 0 || form.full_name.length > 100) {
      toast.error("Name must be 1–100 characters.");
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email) || form.email.length > 255) {
      toast.error("Enter a valid email address.");
      return;
    }

    setSaving(true);
    const toastId = toast.loading("Saving profile…");
    try {
      const payload: Parameters<typeof supabase.auth.updateUser>[0] = {
        data: {
          full_name: form.full_name.trim(),
          timezone: form.timezone.trim(),
          plan: form.plan.trim(),
        },
      };
      if (form.email !== initialEmail) payload.email = form.email.trim();

      const { error } = await supabase.auth.updateUser(payload);
      if (error) throw error;

      if (form.email !== initialEmail) {
        toast.success("Profile saved. Check your new email to confirm the change.", { id: toastId });
      } else {
        toast.success("Profile saved successfully.", { id: toastId });
      }
      setSavedAt(Date.now());
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Profile save failed. Please try again.";
      toast.error(msg, { id: toastId });
    } finally {
      setSaving(false);
    }
  }

  async function handleSignOut(scope?: "global") {
    await supabase.auth.signOut(scope ? { scope } : undefined);
    navigate({ to: "/login", replace: true });
  }

  return (
    <div className="mx-auto max-w-5xl space-y-8">
      <div>
        <div className="text-xs uppercase tracking-[0.2em] text-primary">Account</div>
        <h1 className="mt-2 font-display text-3xl font-semibold">Settings</h1>
      </div>

      <Section title="Profile" icon={<User className="h-4 w-4 text-primary" />}>
        <form onSubmit={handleSubmit} className="space-y-5">
          <div className="flex items-center gap-5 rounded-lg border border-border bg-card/40 p-4">
            <div className="relative h-20 w-20 shrink-0 overflow-hidden rounded-full border border-border bg-gradient-to-br from-primary/30 to-accent/30">
              {avatarUrl ? (
                <img src={avatarUrl} alt="Avatar" className="h-full w-full object-cover" />
              ) : (
                <div className="grid h-full w-full place-items-center text-lg font-semibold text-primary-foreground/80">
                  {(form.full_name || form.email || "?").slice(0, 1).toUpperCase()}
                </div>
              )}
              {avatarUploading && (
                <div className="absolute inset-0 grid place-items-center bg-background/60 backdrop-blur-sm">
                  <Loader2 className="h-5 w-5 animate-spin text-primary" />
                </div>
              )}
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium">Profile photo</div>
              <div className="text-xs text-muted-foreground">PNG, JPEG, WEBP or GIF · up to 5 MB</div>
              <div className="mt-3 flex flex-wrap gap-2">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept={AVATAR_TYPES.join(",")}
                  className="hidden"
                  onChange={handleAvatarChange}
                />
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={avatarUploading || loading}
                  className="inline-flex items-center gap-2 rounded-lg border border-border bg-card/60 px-3 py-2 text-sm hover:bg-card/80 disabled:opacity-60"
                >
                  <Upload className="h-4 w-4" /> {avatarPath ? "Replace" : "Upload"}
                </button>
                {avatarPath && (
                  <button
                    type="button"
                    onClick={handleAvatarRemove}
                    disabled={avatarUploading}
                    className="inline-flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive hover:bg-destructive/20 disabled:opacity-60"
                  >
                    <Trash2 className="h-4 w-4" /> Remove
                  </button>
                )}
              </div>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <Field
              label="Full name"
              value={form.full_name}
              onChange={(v) => update("full_name", v)}
              disabled={loading}
              maxLength={100}
              placeholder="Your name"
            />
            <Field
              label="Email"
              type="email"
              value={form.email}
              onChange={(v) => update("email", v)}
              disabled={loading}
              maxLength={255}
              placeholder="you@example.com"
            />
            <Field
              label="Time zone"
              value={form.timezone}
              onChange={(v) => update("timezone", v)}
              disabled={loading}
              maxLength={64}
            />
            <Field
              label="Plan"
              value={form.plan}
              onChange={(v) => update("plan", v)}
              disabled={loading}
              maxLength={64}
            />
          </div>
          <div className="flex items-center justify-end gap-3">
            {savedAt && !saving && (
              <span className="inline-flex items-center gap-1.5 text-xs text-success">
                <Check className="h-3.5 w-3.5" /> Saved
              </span>
            )}
            <button
              type="submit"
              disabled={saving || loading}
              className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </form>
      </Section>

      <Section title="Binance keys" icon={<KeyRound className="h-4 w-4 text-primary" />}>
        <p className="text-sm text-muted-foreground">
          Binance credentials are loaded from the worker's environment — they
          never touch the web app or database. See the{" "}
          <Link to="/app/connect" className="text-primary hover:underline">
            Connect page
          </Link>{" "}
          for live worker connection status.
        </p>
      </Section>

      <Section title="Danger zone" icon={<ShieldAlert className="h-4 w-4 text-destructive" />} tone="destructive">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-medium">Sign out everywhere</div>
            <div className="text-xs text-muted-foreground">Invalidate all sessions on every device.</div>
          </div>
          <button type="button" onClick={() => handleSignOut("global")} className="inline-flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm font-medium text-destructive hover:bg-destructive/20">
            <LogOut className="h-4 w-4" /> Sign out
          </button>
        </div>
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border/60 pt-4">
          <div>
            <div className="text-sm font-medium">Delete account</div>
            <div className="text-xs text-muted-foreground">Removes all data permanently after a 14-day grace period.</div>
          </div>
          <button className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm font-medium text-destructive hover:bg-destructive/20">
            Delete account
          </button>
        </div>
      </Section>

      <Section title="Support" icon={<Mail className="h-4 w-4 text-primary" />}>
        <p className="text-sm text-muted-foreground">
          Need help with your account or the engine? Reach the team at{" "}
          <a href="#" className="text-primary hover:underline">support@helix.trade</a>.
        </p>
      </Section>
    </div>
  );
}

function Section({ title, icon, children, tone }: { title: string; icon: React.ReactNode; children: React.ReactNode; tone?: "destructive" }) {
  return (
    <div className={`card-elevated p-6 ${tone === "destructive" ? "ring-1 ring-destructive/30" : ""}`}>
      <div className="flex items-center gap-2 text-sm font-medium">{icon}{title}</div>
      <div className="mt-5">{children}</div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  type = "text",
  disabled,
  maxLength,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  disabled?: boolean;
  maxLength?: number;
  placeholder?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-muted-foreground">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        maxLength={maxLength}
        placeholder={placeholder}
        className="w-full rounded-lg border border-border bg-input/40 px-3 py-2.5 text-sm focus:border-primary/60 focus:outline-none focus:ring-2 focus:ring-primary/20 disabled:opacity-60"
      />
    </label>
  );
}

