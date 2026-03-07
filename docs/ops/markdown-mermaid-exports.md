# Markdown + Mermaid Export MVP (Scope Note)

## Goal

Add a minimal CLI flow to export Markdown documents that contain Mermaid code fences into shareable artifacts, with a preference for `PNG` + `PDF`.

## Proposed CLI surface

- Command: `car render markdown <input.md>`
- Default output dir: `.codex-autorunner/filebox/outbox/`
- Default outputs:
  - Mermaid diagrams: `PNG` and `PDF`
  - Document: `HTML` (portable baseline)
- Optional document export: `PDF` via `pandoc` when a PDF engine is available.

## Architecture

1. Read source Markdown.
2. Extract fenced Mermaid blocks (```mermaid ... ```).
3. Render each Mermaid block via external Mermaid CLI (`mmdc` or user-specified command).
4. Create an intermediate Markdown where Mermaid fences are replaced with links to rendered images.
5. Run `pandoc` to emit document exports (`html`, optional `pdf`).
6. Write all outputs to the target directory (default outbox).

## Dependencies

- Required:
  - Mermaid CLI command (`mmdc` or equivalent command string)
  - `pandoc`
- Added Python dependencies: none.

## Tradeoffs

- Pros:
  - Keeps repository dependencies unchanged.
  - Uses standard CLI tools that are easy to inspect/debug.
  - Produces static artifacts suitable for sharing.
- Cons:
  - Relies on external binaries being installed.
  - PDF generation quality/availability depends on local `pandoc` PDF engine setup.
  - MVP intentionally supports fenced Mermaid blocks only (not all Markdown extensions).
