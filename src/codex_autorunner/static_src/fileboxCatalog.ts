export const FILEBOX_BOXES = ["inbox", "outbox"] as const;

export type FileBoxBox = (typeof FILEBOX_BOXES)[number];

export const DEFAULT_FILEBOX_BOX: FileBoxBox = FILEBOX_BOXES[0];
