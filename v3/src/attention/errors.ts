export class AttentionError extends Error {
  constructor(readonly code: string, message: string, readonly status: 400 | 401 | 404 | 409 | 413 = 409) {
    super(message); this.name = "AttentionError";
  }
}
