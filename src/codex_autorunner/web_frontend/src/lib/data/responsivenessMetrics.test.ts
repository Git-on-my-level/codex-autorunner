import { describe, expect, it } from 'vitest';
import { ResponsivenessMetricsRecorder } from './responsivenessMetrics';

describe('responsiveness metrics recorder', () => {
  it('records initial snapshot and first useful paint timings', () => {
    let now = 100;
    const recorder = new ResponsivenessMetricsRecorder(() => now);

    recorder.mark('snapshot:start');
    now = 135.4;
    recorder.measure('initial_snapshot_ms', 'snapshot:start');
    recorder.mark('paint:start');
    now = 149.9;
    recorder.measure('first_useful_paint_ms', 'paint:start');

    expect(recorder.latest('initial_snapshot_ms')).toMatchObject({
      value: 35.4,
      category: 'backend_snapshot_latency'
    });
    expect(recorder.latest('first_useful_paint_ms')).toMatchObject({
      value: 14.5,
      category: 'frontend_render_work'
    });
  });

  it('records patch apply time and long tasks as frontend render work', () => {
    let now = 0;
    const recorder = new ResponsivenessMetricsRecorder(() => now);

    recorder.mark('patch:start');
    now = 8.25;
    recorder.measure('patch_apply_ms', 'patch:start');
    recorder.recordLongTask(49);
    recorder.recordLongTask(51);

    expect(recorder.latest('patch_apply_ms')).toMatchObject({
      value: 8.25,
      category: 'frontend_render_work'
    });
    expect(recorder.latest('long_task_count')).toMatchObject({
      value: 1,
      category: 'frontend_render_work'
    });
  });

  it('records bounded DOM rows from virtualized surfaces', () => {
    const recorder = new ResponsivenessMetricsRecorder(() => 0);

    const count = recorder.recordVirtualizedDomRows('.chat-row', (selector) =>
      selector === '.chat-row' ? 40 : 0
    );

    expect(count).toBe(40);
    expect(recorder.latest('virtualized_dom_rows')).toMatchObject({
      value: 40,
      category: 'frontend_render_work'
    });
  });
});
