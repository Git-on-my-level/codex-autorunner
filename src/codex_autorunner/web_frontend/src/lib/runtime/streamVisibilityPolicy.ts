export type StreamVisibilityPolicy = {
  suspendWhenHidden: boolean;
  isVisible: () => boolean;
  subscribe: (listener: (visible: boolean) => void) => () => void;
};

export type VisibilityDocumentAdapter = {
  visibilityState?: DocumentVisibilityState;
  addEventListener: (type: 'visibilitychange', listener: () => void) => void;
  removeEventListener: (type: 'visibilitychange', listener: () => void) => void;
};

export const alwaysLiveStreamVisibilityPolicy: StreamVisibilityPolicy = {
  suspendWhenHidden: false,
  isVisible: () => true,
  subscribe: () => () => {}
};

export function createDocumentStreamVisibilityPolicy(
  adapter: VisibilityDocumentAdapter | null | undefined = typeof document === 'undefined' ? null : document
): StreamVisibilityPolicy {
  if (!adapter) return alwaysLiveStreamVisibilityPolicy;
  return {
    suspendWhenHidden: true,
    isVisible: () => adapter.visibilityState !== 'hidden',
    subscribe: (listener) => {
      const handleVisibilityChange = () => listener(adapter.visibilityState !== 'hidden');
      adapter.addEventListener('visibilitychange', handleVisibilityChange);
      return () => adapter.removeEventListener('visibilitychange', handleVisibilityChange);
    }
  };
}
