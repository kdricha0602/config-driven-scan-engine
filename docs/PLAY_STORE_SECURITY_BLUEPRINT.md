# Play Store Security Blueprint — "Cyber" Portfolio App

**Status:** Design blueprint (pre-build). All tooling below is free/open-source.
**Date:** 2026-09-20
**Auth-stack verification:** 2026-09-20 — Firebase Auth, Android Credential Manager, and Play Integrity checked against current official docs. One correction resulted: Firebase Authentication has **no native passkey/WebAuthn provider**, so Firebase cannot act as the WebAuthn relying party. Phase 2 below is rewritten with the corrected flow (our backend verifies passkeys; Firebase issues sessions via custom tokens). Every original goal — hosted, passkeys primary, free, no password database — still holds.
**Applies to:** the future Google Play release of the scan-engine-backed app holding users' simulation portfolios and user data.

---

## 1. Threat model

**What we protect:**
1. User credentials and auth tokens (highest value — they unlock everything).
2. Personally identifying information (email, name, device data).
3. Users' simulation portfolios and watchlists (moderate value, high trust value — a leak here kills the product even though no real money is involved).
4. Our backend LLM provider keys (Groq, NVIDIA, Gemini) — a leak here means someone else burns our quota/budget.

**Who we protect against:**
- Casual attackers: decompile the APK, read plaintext prefs/backups, intercept Wi-Fi traffic.
- Motivated attackers: rooted devices, MITM proxies, credential stuffing, API abuse.
- Ourselves: misconfiguration, secrets in repos, unencrypted backups.

**Charter rule (non-negotiable):** the app deals in *simulation* portfolios only. We never request, accept, or store real brokerage credentials. The day we hold a real trading login, this entire threat model changes.

---

## 2. Architecture — the one decision everything hangs on

```
Android app  ──TLS+pinning──▶  API backend (FastAPI, authenticated)
                                    │
                                    ├── Temporal engine (orchestration)
                                    ├── LiteLLM providers (keys live HERE only)
                                    └── Encrypted user database
```

- The Android app holds **zero** provider API keys, zero secrets. It authenticates the *user* and calls *our* API. Anything shipped inside an APK is eventually extractable — so nothing worth stealing ships in it.
- All LLM keys remain server-side in the existing Secure Vault pattern, exactly as the scan engine does today.
- The current `localhost:7233` Temporal dev server must be replaced with a secured deployment (TLS + authentication) before any user traffic touches it.

---

## 3. Backend changes, in build order

### Phase 1 — API foundation (prerequisite for everything)
- [ ] Add an HTTPS API layer (FastAPI) in front of the Temporal engine. The app never talks to Temporal directly.
- [ ] TLS everywhere via **Let's Encrypt** (free, auto-renewing). No cleartext endpoints, ever.
- [ ] Replace the Temporal dev server with a deployment that has TLS + auth enabled.

### Phase 2 — User authentication — **DECIDED 2026-09-20: hosted** (Korben chose hosted over per-user self-hosted IdPs)
- [ ] **Firebase Authentication** (free tier — up to 50K monthly active users, no card required) as the hosted auth provider: user directory, ID-token issuance/verification, and account management live there, not hand-rolled.
- [ ] **Passkeys as the primary login, verified by our backend.** Firebase has no native WebAuthn/passkey provider, so the relying party is our FastAPI backend, not Firebase:
  - Registration: Android `androidx.credentials` Credential Manager `createCredential()` → our `/auth/passkey/register/*` endpoints generate the challenge, verify the attestation (Python `webauthn` package), and store the credential public key + signature counter per user in Postgres. Use discoverable credentials (resident keys) so sign-in needs no username typed first.
  - Sign-in: Credential Manager `getCredential()` → our `/auth/passkey/authenticate/*` endpoints verify the assertion → backend mints a **Firebase custom token** (Admin SDK `create_custom_token`) → app signs in with `signInWithCustomToken()` → Firebase issues its standard ID token (1h) + refresh token. Our API authorizes requests by verifying the Firebase ID token server-side.
  - Phishing-resistant by design; pairs naturally with the biometric gate in section 4.
  - Correction to the first draft: passkey private keys live in the user's credential provider (Google Password Manager) and **sync across that user's devices end-to-end encrypted** — they are not strictly locked to one phone's Keystore. The property we rely on is phishing-resistance, not device-boundedness; the device-bound layer is the BiometricPrompt + Keystore CryptoObject gate in section 4.
- [ ] **Account recovery plan (required before launch):** multiple passkeys per account (phone + backup device — our credential table holds N per user) and/or a one-time printed recovery code stored as an Argon2id hash in our DB. A lost phone with no recovery path = a locked-out user.
- [ ] Sessions: Firebase ID tokens (short-lived, 1h default) verified server-side on every API call; refresh handled by the Firebase SDK with its built-in rotation. No bespoke JWT layer unless a future need forces one.
- [ ] Parked (not default): **bring-your-own self-hosted OIDC provider per user** — rejected for now per Korben's hosted decision; every user running their own IdP is too much friction for a Play Store app.

### Phase 3 — Encryption at rest for user data
- [ ] Application-layer **envelope encryption** for sensitive fields (Python `cryptography` library, free):
  - Data-encryption keys (DEKs) per user or per table; key-encryption keys (KEKs) held in the Secure Vault, never in the database.
  - Encrypt at minimum: auth token hashes, PII columns, portfolio holdings/notes.
  - Simulation portfolio *numbers* are moderate sensitivity — encrypt them too; it's cheap once the envelope pattern exists.
- [ ] Database-level: if using Postgres, enable full-disk encryption on the host *plus* the application layer above (defense in depth, both free).
- [ ] Backups are encrypted with a separate backup key, stored off-site from the data.

### Phase 4 — Abuse and cost protection
- [ ] **Per-user rate limiting** on every endpoint, especially scan-triggering ones — one abusive user must never be able to burn the shared LLM budget. (This is the backend twin of the engine's existing concurrency caps.)
- [ ] Request size caps, timeout ceilings, and idempotency keys on scan requests.
- [ ] Extend the existing append-only audit log (`/tmp/scan-engine-audit.jsonl` pattern) to a persistent store: log auth events, key rotations, and admin actions. Alert on anomalies (e.g., token-reuse revocations spiking).

### Phase 5 — CI / pre-release gates
- [ ] `guard_check.py` (already built) runs on every backend commit — secrets and dangerous calls.
- [ ] `pip-audit` on every backend build — dependency CVEs.
- [ ] **OWASP ZAP** (free) baseline scan against the staging API on every release.

---

## 4. Android client security spec

| Control | Implementation | Cost |
|---|---|---|
| Key storage | **Android Keystore**, hardware-backed; keys generated in-keystore, never pulled into app memory | Free (OS) |
| Encrypted prefs/tokens | **Tink** (Google's crypto library) with Keystore-managed keys; DataStore for settings | Free (open-source) |
| Local database encryption | **SQLCipher for Android** integrated with Room; passphrase derived from a Keystore key | Free (open-source) |
| Network | HTTPS only; **certificate pinning** via OkHttp `CertificatePinner` with **two pins** (current + backup) so rotation never bricks the app | Free |
| Tamper/root detection | **Play Integrity API** — gate high-value actions (login, portfolio decrypt, scan triggers) on its verdicts; cache verdicts per session rather than re-requesting | Free; 10,000 req/day default quota (increase requestable via Play Console) |
| App signing | **Play App Signing** with the split-key model; upload key backed up in two places, never in the repo | Free (required) |
| Build hardening | R8 minification + resource shrinking; logging stripped from release builds; keep and upload `mapping.txt` | Free (built-in) |
| Manifest | `android:allowBackup="false"` (blocks `adb backup` extraction), `cleartextTrafficPermitted="false"` | Free |
| Sensitive actions | **BiometricPrompt with a Keystore CryptoObject** gating the portfolio view — decryption literally requires the biometric, so client-side bypass is useless | Free (OS) |
| Permissions | Least privilege; audit the merged manifest every release for SDK-injected surprises | Free |
| Secrets | None in the APK. No API keys in code, resources, or `strings.xml`. Remote config for non-sensitive tuning only | Free (discipline) |

---

## 5. Data classification & handling matrix

| Data | Sensitivity | In transit | At rest (device) | At rest (server) |
|---|---|---|---|---|
| Auth/refresh tokens | Critical | TLS + pinning | Tink/Keystore | Hashed; short-lived |
| Passkey credential public keys | Low-Medium | TLS + pinning | n/a (server-side) | Postgres; public keys only — useless without the private key |
| Password (if ever local) | Critical | TLS + pinning | Never stored | Argon2id hash only |
| Email / PII | High | TLS + pinning | SQLCipher | Envelope-encrypted column |
| Simulation portfolios | Medium | TLS + pinning | SQLCipher | Envelope-encrypted |
| Scan results / market data | Low | TLS + pinning | Plain cache OK | Plain OK |
| LLM provider keys | Critical | N/A (server only) | N/A — never on device | Secure Vault only |

Retention: delete user data on account deletion, including backups within the backup rotation window. Say so in the privacy policy — it's also a Play Store listing requirement.

---

## 6. Verification — prove it before every release

Test against **OWASP MASVS v2.1.0** (the industry baseline; eight control groups: STORAGE, CRYPTO, AUTH, NETWORK, PLATFORM, CODE, RESILIENCE, PRIVACY):

- [ ] **MobSF** (free, open-source) static+dynamic scan of every release APK in CI, mapped to MASVS controls. This is the Android counterpart to the backend's `guard_check.py`.
- [ ] Manual pass with **jadx/apktool** on the release APK: grep for hardcoded secrets, check for plaintext storage, verify no sensitive data in `logcat`.
- [ ] **Frida/objection** (free) runtime check on a test device: confirm tokens never appear in plaintext memory/files and pinning can't be trivially bypassed without detection.
- [ ] Release-gate rule: **no green MobSF + ZAP pass, no Play upload.** Boring, automatic, non-negotiable.

---

## 7. Operational discipline (the part money can't buy)

- **Key rotation schedule:** KEKs yearly, JWT signing keys on any suspected leak, backup pins rotated before cert expiry. Calendar it.
- **Dependency updates:** monthly `pip-audit` (backend) and Gradle dependency review (Android); emergency patch on any critical CVE in the auth/crypto path.
- **Incident plan (one page):** if a key leaks — revoke, rotate, force re-login, notify. Written *before* it's needed.
- **No silent security downgrades:** any exception to this blueprint (e.g., temporarily disabling pinning) gets a dated entry here with an expiry date.

---

## 8. Cost summary

Everything above is free: Let's Encrypt, Firebase Auth free tier (50K MAU, no card required), Tink, SQLCipher, Play Integrity API (10k req/day default), Play App Signing, R8, MobSF, ZAP, OWASP MASVS/MASTG, `cryptography`, Argon2. The only real costs are hosting for the secured backend and the one-time Google Play developer account fee. Security here is paid for in discipline, not dollars.

---

## 9. Open decisions (need Korben's call when build starts)

1. ~~Firebase Auth vs. fully self-hosted auth~~ — **DECIDED 2026-09-20: hosted** (Firebase Auth free tier, passkeys as primary login method).
2. ~~Hosting for the secured backend (VPS vs. managed)~~ — **DECIDED 2026-09-20: full free failover stack** — Render (primary) → Google Cloud Run (hot standby) → Oracle Always Free ARM (last-resort tank), one shared Supabase Postgres, Postgres-advisory-lock leader election, GitHub Actions watchdog. Full design in `FREE_FAILOVER_STACK.md`.
3. Whether simulation portfolios sync across devices (server source of truth) or stay device-local with optional backup — this changes the encryption design.
