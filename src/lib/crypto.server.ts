// AES-256-GCM helpers for Binance key encryption. Server-only.
// Key is derived from BINANCE_KEY_ENCRYPTION_SECRET via SHA-256.
import { createCipheriv, createDecipheriv, randomBytes, createHash } from "crypto";

function getKey(): Buffer {
  const secret = process.env.BINANCE_KEY_ENCRYPTION_SECRET;
  if (!secret) throw new Error("BINANCE_KEY_ENCRYPTION_SECRET is not set");
  return createHash("sha256").update(secret).digest();
}

// Returns a Buffer: [12B iv][16B tag][ciphertext]
export function encryptString(plain: string): Buffer {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", getKey(), iv);
  const ct = Buffer.concat([cipher.update(plain, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, ct]);
}

export function decryptBuffer(buf: Buffer): string {
  const iv = buf.subarray(0, 12);
  const tag = buf.subarray(12, 28);
  const ct = buf.subarray(28);
  const decipher = createDecipheriv("aes-256-gcm", getKey(), iv);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ct), decipher.final()]).toString("utf8");
}
