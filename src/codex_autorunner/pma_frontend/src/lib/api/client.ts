export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; message: string };

export class PmaApiClient {
  constructor(private readonly fetcher: typeof fetch = fetch) {}

  async getJson<T>(path: string): Promise<ApiResult<T>> {
    const response = await this.fetcher(path, {
      headers: { accept: 'application/json' }
    });

    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        message: response.statusText || `Request failed with ${response.status}`
      };
    }

    return { ok: true, data: (await response.json()) as T };
  }
}

export const pmaApi = new PmaApiClient();
