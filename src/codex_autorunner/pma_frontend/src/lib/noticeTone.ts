/** Maps short-lived status copy to notice styling. */
export function noticeTone(message: string | null): 'neutral' | 'success' | 'warning' | 'danger' {
  if (!message) return 'neutral';
  const normalized = message.toLowerCase();
  if (normalized.includes('failed') || normalized.includes('could not') || normalized.includes('error')) return 'danger';
  if (normalized.includes('cannot') || normalized.includes('unavailable') || normalized.includes('only numbered')) return 'warning';
  if (normalized.includes('saved') || normalized.includes('created') || normalized.includes('updated') || normalized.includes('accepted'))
    return 'success';
  return 'neutral';
}
