/**
 * API client.
 *
 * Two things worth knowing about this file:
 *
 * 1. Tokens live in the OS keychain via @capacitor/preferences, not in
 *    localStorage. On a device that is genuinely shared — a factory office
 *    tablet — localStorage is readable by anything that can run script in the
 *    webview.
 *
 * 2. A 401 means the token was revoked, not merely expired. STACOS puts the
 *    user's security stamp in a `sec` claim and checks it on every request, so
 *    "sign out everywhere", a role change or a password change invalidates this
 *    session on its next call. Refreshing after such a 401 is pointless — the
 *    refresh token carries the same stale stamp — so we sign out instead.
 */

import { Preferences } from "@capacitor/preferences";

const ACCESS_KEY = "stacos.access";
const REFRESH_KEY = "stacos.refresh";

// Empty in development: Vite proxies /api to Django on :8000. A native build
// sets this at build time to the real host.
const BASE = import.meta.env.VITE_API_BASE ?? "";

let accessToken = null;
let onSignedOut = () => {};

export function setSignOutHandler(handler) {
  onSignedOut = handler;
}

// ---------------------------------------------------------------------------
// Token storage
// ---------------------------------------------------------------------------

export async function loadTokens() {
  const { value: access } = await Preferences.get({ key: ACCESS_KEY });
  accessToken = access ?? null;
  return accessToken;
}

async function storeTokens({ access, refresh }) {
  accessToken = access;
  await Preferences.set({ key: ACCESS_KEY, value: access });
  if (refresh) await Preferences.set({ key: REFRESH_KEY, value: refresh });
}

export async function clearTokens() {
  accessToken = null;
  await Preferences.remove({ key: ACCESS_KEY });
  await Preferences.remove({ key: REFRESH_KEY });
}

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

async function request(path, { method = "GET", body, auth = true, retry = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth && accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const response = await fetch(`${BASE}/api/v1${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (response.status === 401 && auth && retry) {
    // One attempt to refresh. If the stamp was rotated this fails too, and the
    // user is signed out — which is the intended behaviour, not a failure.
    const refreshed = await tryRefresh();
    if (refreshed) return request(path, { method, body, auth, retry: false });
    await clearTokens();
    onSignedOut();
    throw new ApiError("Your session has ended. Please sign in again.", 401);
  }

  const payload = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new ApiError(payload.detail ?? "Something went wrong.", response.status, payload);
  }
  return payload;
}

async function tryRefresh() {
  const { value: refresh } = await Preferences.get({ key: REFRESH_KEY });
  if (!refresh) return false;

  const response = await fetch(`${BASE}/api/v1/auth/refresh/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh }),
  });
  if (!response.ok) return false;

  await storeTokens(await response.json());
  return true;
}

export class ApiError extends Error {
  constructor(message, status, payload = {}) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export const api = {
  /**
   * Step one. Returns a verification id — never a token.
   *
   * Every sign-in from the app is treated as a new device: there is no
   * trusted-device cookie here. Device trust on mobile is the keychain holding
   * the refresh token, which is a stronger guarantee than a browser cookie.
   */
  login: (email, password) =>
    request("/auth/login/", { method: "POST", body: { email, password }, auth: false }),

  /** Step two: BOTH codes together. Neither channel can be satisfied alone. */
  async verify(verificationId, emailCode, phoneCode) {
    const tokens = await request("/auth/verify/", {
      method: "POST",
      body: {
        verification_id: verificationId,
        email_code: emailCode,
        phone_code: phoneCode,
      },
      auth: false,
    });
    await storeTokens(tokens);
    return tokens;
  },

  async logout() {
    const { value: refresh } = await Preferences.get({ key: REFRESH_KEY });
    if (refresh) {
      await request("/auth/logout/", { method: "POST", body: { refresh } }).catch(() => {});
    }
    await clearTokens();
  },

  me: () => request("/me/"),
};
