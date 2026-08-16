const config = window.RETAILFIXIT_CONFIG;
const TOKEN_KEY = "retailfixit-cognito-session";
const VERIFIER_KEY = "retailfixit-pkce-verifier";
const STATE_KEY = "retailfixit-pkce-state";

function base64Url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function randomString() {
  return base64Url(crypto.getRandomValues(new Uint8Array(32)));
}

async function challengeFor(verifier) {
  return base64Url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
}

export function isConfigured() {
  return Boolean(config.apiBaseUrl && config.cognitoDomain && config.cognitoClientId);
}

export function currentSession() {
  try {
    const session = JSON.parse(sessionStorage.getItem(TOKEN_KEY) || "null");
    if (!session || Date.now() > session.expiresAt) return null;
    return session;
  } catch {
    return null;
  }
}

export async function startLogin() {
  if (!isConfigured()) throw new Error("Cognito configuration is missing.");
  const verifier = randomString();
  const state = randomString();
  sessionStorage.setItem(VERIFIER_KEY, verifier);
  sessionStorage.setItem(STATE_KEY, state);
  const challenge = await challengeFor(verifier);
  const params = new URLSearchParams({
    client_id: config.cognitoClientId,
    response_type: "code",
    scope: "openid email",
    redirect_uri: config.redirectUri,
    code_challenge_method: "S256",
    code_challenge: challenge,
    state,
  });
  window.location.assign(`${config.cognitoDomain}/oauth2/authorize?${params}`);
}

export async function finishLogin() {
  const query = new URLSearchParams(window.location.search);
  const code = query.get("code");
  if (!code) return currentSession();
  const expectedState = sessionStorage.getItem(STATE_KEY);
  if (!expectedState || expectedState !== query.get("state")) {
    throw new Error("Cognito login state validation failed.");
  }
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: config.cognitoClientId,
    code,
    redirect_uri: config.redirectUri,
    code_verifier: sessionStorage.getItem(VERIFIER_KEY) || "",
  });
  const response = await fetch(`${config.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  const rawBody = await response.text();
  let tokens = null;
  try {
    tokens = rawBody ? JSON.parse(rawBody) : null;
  } catch {
    throw new Error(
      `Cognito token exchange returned HTTP ${response.status} with a non-JSON response.`
    );
  }
  if (!response.ok) {
    throw new Error(
      tokens?.error_description ||
        tokens?.error ||
        `Cognito token exchange failed with HTTP ${response.status}.`
    );
  }
  if (!tokens?.id_token || !tokens?.access_token) {
    throw new Error("Cognito token exchange returned an incomplete session.");
  }
  const session = {
    accessToken: tokens.access_token,
    idToken: tokens.id_token,
    expiresAt: Date.now() + (tokens.expires_in * 1000) - 30000,
  };
  sessionStorage.setItem(TOKEN_KEY, JSON.stringify(session));
  sessionStorage.removeItem(VERIFIER_KEY);
  sessionStorage.removeItem(STATE_KEY);
  window.history.replaceState({}, document.title, window.location.pathname);
  return session;
}

export function signOut() {
  sessionStorage.removeItem(TOKEN_KEY);
  if (isConfigured()) {
    const params = new URLSearchParams({
      client_id: config.cognitoClientId,
      logout_uri: config.redirectUri,
    });
    window.location.assign(`${config.cognitoDomain}/logout?${params}`);
  } else {
    window.location.reload();
  }
}

/**
 * FastAPI returns `detail` as a string for HTTPException but as a list of
 * error objects for request-validation failures. Rendering the list directly
 * produced "[object Object]" for every 422.
 */
function formatDetail(detail, status) {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => {
      const field = Array.isArray(item.loc)
        ? item.loc.filter((part) => part !== "body").join(".")
        : "";
      return field ? `${field}: ${item.msg}` : item.msg;
    });
    return messages.join(" · ") || `AWS API returned ${status}`;
  }
  return `AWS API returned ${status}`;
}

export async function apiRequest(path, options = {}) {
  const session = currentSession();
  if (!session) throw new Error("Your session has expired. Sign in again.");
  const response = await fetch(`${config.apiBaseUrl}${path}`, {
    ...options,
    headers: {
      "content-type": "application/json",
      // API Gateway validates the Cognito ID token audience for this app client.
      Authorization: `Bearer ${session.idToken}`,
      ...(options.headers || {}),
    },
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    if (response.status === 401) {
      sessionStorage.removeItem(TOKEN_KEY);
      throw new Error("Your session is no longer valid. Sign in again.");
    }
    throw new Error(formatDetail(data?.detail, response.status));
  }
  return data;
}

export function identityFromSession() {
  const session = currentSession();
  if (!session?.idToken) return "Cognito user";
  try {
    const payload = JSON.parse(atob(session.idToken.split(".")[1]));
    return payload.email || payload.sub || "Cognito user";
  } catch {
    return "Cognito user";
  }
}
