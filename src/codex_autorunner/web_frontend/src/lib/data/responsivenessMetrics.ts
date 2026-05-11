export type ResponsivenessMetricName =
  | 'initial_snapshot_ms'
  | 'first_useful_paint_ms'
  | 'patch_apply_ms'
  | 'long_task_count'
  | 'virtualized_dom_rows';

export type ResponsivenessMetric = {
  name: ResponsivenessMetricName;
  value: number;
  category: 'backend_snapshot_latency' | 'frontend_render_work';
};

export type DomCountProvider = (selector: string) => number;

export class ResponsivenessMetricsRecorder {
  private marks = new Map<string, number>();
  private readonly metrics: ResponsivenessMetric[] = [];

  constructor(private readonly now: () => number = () => performance.now()) {}

  mark(name: string): void {
    this.marks.set(name, this.now());
  }

  measure(name: Extract<ResponsivenessMetricName, 'initial_snapshot_ms' | 'first_useful_paint_ms' | 'patch_apply_ms'>, start: string, end?: string): number {
    const startedAt = this.marks.get(start);
    const finishedAt = end ? this.marks.get(end) : this.now();
    if (startedAt === undefined || finishedAt === undefined) return 0;
    const value = Math.max(0, finishedAt - startedAt);
    this.record(name, value);
    return value;
  }

  recordLongTask(durationMs: number, thresholdMs = 50): void {
    if (durationMs >= thresholdMs) {
      const current = this.latest('long_task_count')?.value ?? 0;
      this.record('long_task_count', current + 1);
    }
  }

  recordVirtualizedDomRows(selector: string, countProvider: DomCountProvider = browserDomCount): number {
    const count = countProvider(selector);
    this.record('virtualized_dom_rows', count);
    return count;
  }

  record(name: ResponsivenessMetricName, value: number): void {
    const metric: ResponsivenessMetric = {
      name,
      value: roundMetric(value),
      category: name === 'initial_snapshot_ms' ? 'backend_snapshot_latency' : 'frontend_render_work'
    };
    const existingIndex = this.metrics.findIndex((item) => item.name === name);
    if (existingIndex >= 0) this.metrics[existingIndex] = metric;
    else this.metrics.push(metric);
  }

  latest(name: ResponsivenessMetricName): ResponsivenessMetric | null {
    return this.metrics.find((item) => item.name === name) ?? null;
  }

  snapshot(): ResponsivenessMetric[] {
    return this.metrics.map((item) => ({ ...item }));
  }
}

export function observeLongTasks(
  recorder: ResponsivenessMetricsRecorder,
  PerformanceObserverCtor: typeof PerformanceObserver | undefined = typeof PerformanceObserver === 'undefined' ? undefined : PerformanceObserver
): { disconnect(): void } | null {
  if (!PerformanceObserverCtor) return null;
  const observer = new PerformanceObserverCtor((list) => {
    for (const entry of list.getEntries()) {
      recorder.recordLongTask(entry.duration);
    }
  });
  observer.observe({ entryTypes: ['longtask'] });
  return observer;
}

function browserDomCount(selector: string): number {
  if (typeof document === 'undefined') return 0;
  return document.querySelectorAll(selector).length;
}

function roundMetric(value: number): number {
  return Math.round(Math.max(0, value) * 1000) / 1000;
}
