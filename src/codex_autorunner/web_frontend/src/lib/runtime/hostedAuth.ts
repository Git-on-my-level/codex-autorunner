type HostedBearerState = {
  token: string;
  expiresAt: number | null;
};

const ACCESS_TOKEN_PARAM = 'car_access_token';
const EXPIRES_AT_PARAM = 'car_access_token_expires_at';

let state: HostedBearerState | null = null;

export function consumeHostedBearerFromLocation(location: Location = window.location): boolean {
  const hash = location.hash.startsWith('#') ? location.hash.slice(1) : location.hash;
  if (!hash) return false;
  const params = new URLSearchParams(hash);
  const token = params.get(ACCESS_TOKEN_PARAM)?.trim() ?? '';
  if (!token) return false;

  const expiresAtRaw = params.get(EXPIRES_AT_PARAM);
  const expiresAt = expiresAtRaw ? Number.parseInt(expiresAtRaw, 10) : Number.NaN;
  setHostedBearer(token, Number.isFinite(expiresAt) ? expiresAt : null);
  params.delete(ACCESS_TOKEN_PARAM);
  params.delete(EXPIRES_AT_PARAM);

  const nextHash = params.toString();
  const nextUrl = `${location.pathname}${location.search}${nextHash ? `#${nextHash}` : ''}`;
  window.history.replaceState(null, document.title, nextUrl);
  return true;
}

export function setHostedBearer(token: string, expiresAt: number | null = null): void {
  state = { token, expiresAt };
}

export function clearHostedBearer(): void {
  state = null;
}

export function hostedBearerToken(): string | null {
  if (!state) return null;
  if (state.expiresAt !== null && state.expiresAt * 1000 <= Date.now()) {
    state = null;
    return null;
  }
  return state.token;
}

export function hostedAuthorizationHeader(): string | null {
  const token = hostedBearerToken();
  return token ? `Bearer ${token}` : null;
}
