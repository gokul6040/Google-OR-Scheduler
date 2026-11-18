# Portal accounts

Accounts live alongside the selected application's clinical tables in `portal_users`,
`portal_sessions`, `portal_login_limits`, `portal_audit`, and `portal_auth_control`.
No email provider or SSO is required. Email is a unique, case-insensitive username.

## Prepare and create the first admin

Run from the repository root against the **verified Thanal target**, with its
database URL in an environment variable. The command requires the variable name
explicitly and does not fall back to the root `.env` (which may target another environment).
Never put credentials directly in shell arguments or commit them.

```powershell
python -m main_workflow.accounts --database-env THERAPY_SCHEDULER_DATABASE_URL --prepare --first-admin
```

The command asks for the admin's name and email, then prompts for a permanent
password with hidden input. Bootstrap refuses if any admin account already exists.
After bootstrap, admins create further accounts through `/users`.
Back up account tables with the rest of the database. Do not copy accounts between
environments without deliberate review.

## Enable sign-in

- `PORTAL_AUTH_ENABLED=true` requires individual login across the portal, including
  `/workflow`, `/test`, OAuth configuration and APIs. It takes precedence over old
  shared-key and local-no-auth settings. Disabled by default for controlled rollout.
- Use HTTPS in production. Cookies are Secure, HttpOnly, SameSite=Lax and expire
  after 12 hours. Tokens are random and only token hashes are stored server-side.
- `PORTAL_LOCAL_HTTP=true` permits cookies over HTTP **only on a server bound to
  loopback** for local development. Never use it for a deployed server.
- If HTTP automation calls the portal, provision a new, independent secret in
  `PORTAL_MACHINE_TOKEN` and update those callers to send `Authorization: Bearer ...`.
  Machine authentication cannot manage accounts or use doctor endpoints. Existing
  internal workflow credentials remain internal; the old shared key alone no longer
  grants browser/API access when portal authentication is enabled.
- Background jobs using the database directly and signed WhatsApp webhooks continue
  independently of browser sessions. No messaging/provider settings are enabled by
  account setup.

## Roles and lifecycle

Admins have all centres and all portal controls. Users creates accounts;
Existing users manages accounts and sits between Users and Change password in
the shared workspace sidebar. Activity records remain stored, but recent activity
is not displayed in the interface.
Doctors are linked to one active clinician; every request checks both account and
clinician status. They can only read their own Today/Tomorrow appointments (India
time), with minimal patient identity, therapy, time, location and status fields.
No clinician ID supplied by the browser controls this query.

Admins can **Edit** an account's name, email and clinician link (the role never changes);
for example, hand a test account to the real doctor by changing its email, then Reset password.
Emails stay unique, an active doctor must stay linked to a free active clinician, and every
edit signs that account out. **Deactivating a doctor account frees its clinician** so a new
account can be created; reactivating it requires linking a free clinician first (Edit).

New accounts and admin password resets generate a temporary password shown once.
First login requires a different permanent password. Passwords use PBKDF2-SHA256
with 600,000 rounds and random salts. Chosen passwords need 8–128 characters with an uppercase
letter, a lowercase letter and a special character, and no spaces. The rule
applies when a password is set or changed; existing passwords keep working until changed.
No passwords or tokens are written to activity records or browser storage.

Password changes, resets, activation and deactivation revoke all of that account's
sessions. Forgotten passwords are handled by another admin. The last active admin
cannot be deactivated; keep a second admin and an operational database recovery
procedure. Resetting a password also clears that account's failed-login limit.
Five failed attempts per account or 50 per client IP within 15 minutes temporarily
block further attempts. Behind Azure Container Apps every request arrives from the platform
ingress, so set `PORTAL_TRUST_FORWARDED_FOR=true` on the deployed app: the per-address limit
then uses the LAST `X-Forwarded-For` entry (appended by the ingress; visitor-written entries can
only appear earlier). Without it, one person's wrong passwords would block all staff. Leave it
unset locally, where the header is ignored.

Successful portal mutations are recorded with signed-in email and route. User
management and authentication have dedicated audit events. Existing clinical audit
records remain in place, and shared workflow cancellation/status actions also use
the signed-in email. No patient data or passwords are included in the new audit.

## Rollout

Verify on an isolated copy first: cross-doctor access, all admin APIs, password
change, reset, deactivation, lockout, same-origin checks and automation credentials.
Prepare production account storage and bootstrap the first admin before enabling
the flag. Do not open public access during migration. Restart the web process with
the flag enabled; verify roles over HTTPS. This feature does not deploy itself or
modify production configuration.

When starting locally with settings resolved directly from the verified Thanal
deployment, set `THERAPY_SCHEDULER_LOAD_LOCAL_ENV=false`. This prevents stale
root or test `.env` files from adding credentials for another environment.
