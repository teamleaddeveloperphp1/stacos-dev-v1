# STACOS mobile

Framework7 + React, wrapped with Capacitor. The **only** client in the system that speaks JSON: it consumes the DRF API at `/api/v1` from the same Django project. There is no mobile-specific backend and no duplicated business logic — every rule it enforces lives in a shared Python service the web views call too.

## Running

```bash
npm install
npm run dev          # http://localhost:3002, proxying /api to :8000
```

Django must be running on `:8000`. The Vite dev server proxies `/api` there, so the browser sees one origin and CSRF/CORS stay out of the way.

## What belongs here, and what does not

Mobile is for **acting and responding**, not administration. The jobs it exists to do:

1. Today / this-week compliance view, red-amber-green.
2. Approve or reject a return or a document, with the summary and step-up auth.
3. Respond to an information request — including capturing a document with the camera.
4. Notices: what arrived, the deadline countdown, forward to the professional.
5. **Close an internal compliance with photo evidence and a geotag** — a fire extinguisher refill, a safety drill, an inspection. This is the genuinely mobile-native case, and the reason the app exists at all.
6. Timer start/stop for practice staff.
7. Push notifications with deep links.

Deliberately **not** here: return preparation, template authoring, billing configuration, user administration. Those are desk work, and cramming them onto a phone produces a worse version of both clients.

## Offline

Reads and evidence capture are offline-first. Uploads queue and sync when connectivity returns, and the sync state is shown honestly rather than optimistically — factory floors and audit sites have no signal, and a photo that silently failed to upload is worse than one visibly waiting.

## Authentication

Two steps, mirroring the web:

1. `POST /api/v1/auth/login/` with email and password → returns a `verification_id`.
2. `POST /api/v1/auth/verify/` with **both** the email code and the phone code → returns an access/refresh pair.

Tokens carry a `sec` claim holding the user's security stamp. Rotating that stamp — "sign out everywhere", a role change, a password change — invalidates outstanding tokens on their next request rather than at expiry. Without that claim, signing out everywhere would quietly do nothing to this app for up to fifteen minutes.

Refresh tokens live in the OS keychain via `@capacitor/preferences`, which is a stronger guarantee than the browser cookie the web client uses for device trust.

## Native builds

```bash
npx cap add android
npx cap add ios
npm run cap:android
```
