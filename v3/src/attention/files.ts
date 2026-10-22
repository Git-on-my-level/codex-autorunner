/** Crash-safe private files for client spools and generated credentials. */
import { closeSync, fsyncSync, linkSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { randomUUID } from "node:crypto";
export function privateDirectory(path: string): void {
  mkdirSync(path, { recursive: true, mode: 0o700 });
  const info = lstatSync(path);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error(`Expected a private directory: ${path}`);
}
export function syncDirectory(path: string): void {
  let fd: number | undefined;
  try { fd = openSync(path, "r"); fsyncSync(fd); }
  catch (error) {
    if (!(["EINVAL", "ENOTSUP", "EPERM", "EISDIR"].includes((error as NodeJS.ErrnoException).code ?? ""))) throw error;
  } finally { if (fd !== undefined) closeSync(fd); }
}
/** Returns false on an existing destination; never overwrites a competing request. */
export function writePrivateJson(path: string, value: unknown, replace = false): boolean {
  privateDirectory(dirname(path));
  const temp = `${path}.${randomUUID()}.tmp`;
  const fd = openSync(temp, "wx", 0o600);
  try { writeFileSync(fd, JSON.stringify(value, null, 2) + "\n"); fsyncSync(fd); }
  catch (error) { try { unlinkSync(temp); } catch {} throw error; }
  finally { closeSync(fd); }
  try {
    if (replace) renameSync(temp, path);
    else {
      try { linkSync(temp, path); }
      catch (error) { if ((error as NodeJS.ErrnoException).code === "EEXIST") return false; throw error; }
    }
    syncDirectory(dirname(path));
    return true;
  } finally { try { unlinkSync(temp); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; } }
}
export function readPrivateJson(path: string): unknown {
  const info = lstatSync(path);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 1024 * 1024) throw new Error(`Invalid private JSON file: ${path}`);
  if (process.platform !== "win32" && ((info.mode & 0o077) !== 0 || (process.getuid && info.uid !== process.getuid()))) {
    throw new Error(`Private file must be owned by this user and chmod 600: ${path}`);
  }
  return JSON.parse(readFileSync(path, "utf8"));
}
export function removeDurably(path: string): void {
  try { unlinkSync(path); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  syncDirectory(dirname(path));
}
