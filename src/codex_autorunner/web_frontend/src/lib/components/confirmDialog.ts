import { writable } from 'svelte/store';

export type ConfirmOptions = {
  message: string;
  title?: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  requireType?: string | null;
};

export type ConfirmPending = ConfirmOptions & {
  resolve: (ok: string | boolean) => void;
};

export const confirmPendingStore = writable<ConfirmPending | null>(null);

export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    confirmPendingStore.set({
      ...options,
      resolve: (value) => resolve(Boolean(value))
    });
  });
}

export function confirmDialogTyped(
  options: ConfirmOptions & { requireType: string }
): Promise<string | null> {
  return new Promise((resolve) => {
    confirmPendingStore.set({
      ...options,
      resolve: (value) => resolve(typeof value === 'string' ? value : null)
    });
  });
}
