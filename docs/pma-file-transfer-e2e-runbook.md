# PMA File Transfer E2E Verification Runbook

This runbook describes the end-to-end verification steps for PMA (Project Management Agent) file transfer flows on both web and Telegram surfaces.

## Prerequisites

- Codex Autorunner hub server running (`car hub serve`)
- Web UI accessible at the hub root
- Telegram bot configured and running
- PMA enabled in hub config (default: `pma.enabled: true`)
- Test files ready:
  - Small text file (< 100 MB)
  - Medium file (e.g., 1-5 MB)
  - Various file types (txt, pdf, png, etc.)

## PMA File Locations

All PMA files live under the hub root:
```
<hub_root>/.codex-autorunner/pma/
├── inbox/    # Files uploaded by users
└── outbox/   # Files written by agents for users to download
```

## PMA Durable Docs (Manual Mode)

PMA also maintains hub-scoped docs under `.codex-autorunner/pma/`:

- `AGENTS.md` for durable guidance and defaults.
- `active_context.md` for short-lived working context.
- `context_log.md` for append-only snapshots when `active_context.md` is pruned.

In the web UI, switch PMA to manual mode to open the docs editor. Use "Save" to write changes and "Snapshot" to append a timestamped copy of `active_context.md` into `context_log.md`.

## Configuration Limits

| Setting | Default | Description |
|---------|---------|-------------|
| `pma.max_upload_bytes` | 10,000,000 | Max file size for web uploads (10 MB) |
| `telegram.media.max_file_bytes` | 100,000,000 | Max file size for Telegram (100 MB) |
| `telegram.media.max_image_bytes` | 5,000,000 | Max image size for Telegram (5 MB) |

---

## Web PMA Verification

### 1. Upload File → File Visible in Inbox

1. Open the web UI at `http://<hub-root>/`
2. Navigate to the PMA chat tab (Hub → PMA)
3. Locate the file upload area (near the chat input)
4. Click "Upload" or drag-and-drop a test file
5. **Expected:**
   - File appears in the Inbox panel
   - File shows correct name, size, and timestamp
   - No error messages displayed

**Verify via file system:**
```bash
ls -la <hub_root>/.codex-autorunner/pma/inbox/
```
The uploaded file should be present.

**Verify via API:**
```bash
curl -H "Authorization: Bearer <token>" \
  http://<hub-root>/hub/pma/files
```
Response should include the file in `inbox` array.

---

### 2. Agent References Uploaded File

1. In the PMA chat, send a message asking the agent to read the uploaded file:
   ```
   Read the file <filename> from the PMA inbox and tell me its contents.
   ```
2. **Expected:**
   - Agent acknowledges the file
   - Agent reads and processes the file contents
   - Agent responds with file information

**Note:** The agent is instructed that user files are in `.codex-autorunner/pma/inbox/`.

---

### 3. Agent Writes Outbox File → UI Detects → Download Works

1. In the PMA chat, ask the agent to write a file:
   ```
   Create a file called response.txt in the PMA outbox with the text "Hello from agent".
   ```
2. **Expected:**
   - Agent completes the request
   - Outbox panel refreshes automatically (or click refresh)
   - File appears in Outbox panel with correct metadata

3. Click the download button/link for the outbox file
4. **Expected:**
   - File downloads successfully
   - Downloaded file content matches what the agent wrote
   - File name is preserved

**Verify via file system:**
```bash
ls -la <hub_root>/.codex-autorunner/pma/outbox/
cat <hub_root>/.codex-autorunner/pma/outbox/response.txt
```

---

### 4. Delete Files from Inbox/Outbox

1. Locate a file in the Inbox or Outbox panel
2. Click the delete button (trash icon)
3. **Expected:**
   - File is removed from the panel
   - No error messages

**Verify via API:**
```bash
curl -X DELETE \
  -H "Authorization: Bearer <token>" \
  http://<hub-root>/hub/pma/files/inbox/<filename>
```

---

## Telegram PMA Verification

### 1. Enable PMA Mode

1. In a Telegram chat with the bot, run:
   ```
   /pma on
   ```
2. **Expected:**
   - Bot confirms PMA mode is enabled
   - Message indicates "Back to repo mode" with `/pma off` hint

**Verify topic is in PMA mode:**
```bash
# Check the bot's topic state (requires access to state store)
# The topic record should have pma_enabled=true
```

---

### 2. Upload File → File Lands in PMA Inbox

1. In the Telegram chat, send a file (any type)
2. **Expected:**
   - Bot confirms file receipt
   - Bot shows file details (name, size, saved path)
   - File is saved to `<hub_root>/.codex-autorunner/pma/inbox/`

**Verify via file system:**
```bash
ls -la <hub_root>/.codex-autorunner/pma/inbox/
```

**Verify via `/files` command:**
```
/files inbox
```
**Expected:** Listing shows the uploaded file.

---

### 3. Agent References Uploaded File

1. After uploading a file, send a chat message:
   ```
   Read the file <filename> from the PMA inbox and summarize it.
   ```
2. **Expected:**
   - Agent reads the file from `.codex-autorunner/pma/inbox/`
   - Agent responds with file contents or summary

---

### 4. Agent Writes Outbox File → `/files outbox` Shows It

1. Ask the agent to write a file:
   ```
   Create a file telegram-output.txt in the PMA outbox with text "Telegram test".
   ```
2. **Expected:**
   - Agent completes the request
   - No errors reported

3. Check the outbox:
   ```
   /files outbox
   ```
4. **Expected:**
   - Outbox shows the new file
   - File name and size are displayed

**Verify via file system:**
```bash
ls -la <hub_root>/.codex-autorunner/pma/outbox/
cat <hub_root>/.codex-autorunner/pma/outbox/telegram-output.txt
```

---

### 5. Send Outbox File via Telegram

1. After creating an outbox file, send:
   ```
   /files send <filename>
   ```
2. **Expected:**
   - Bot sends the file as a document in the chat
   - File is downloaded successfully by the user
   - Bot confirms "Sent."

3. Verify file content matches what was written
4. **Expected:** File content is identical to the outbox file

---

### 6. Clear PMA Inbox/Outbox

1. Clear the inbox:
   ```
   /files clear inbox
   ```
2. **Expected:**
   - Bot reports number of files deleted
   - Inbox is now empty

3. Clear the outbox:
   ```
   /files clear outbox
   ```
4. **Expected:**
   - Bot reports number of files deleted
   - Outbox is now empty

---

## Cross-Surface Verification

### 1. File Uploaded via Telegram Visible on Web PMA

1. In Telegram (PMA mode), upload a file named `cross-surface-test.txt`
2. **Expected:** File lands in `.codex-autorunner/pma/inbox/`

3. Open the web UI and navigate to PMA
4. Click refresh on the Inbox panel (or wait for auto-refresh)
5. **Expected:**
   - File `cross-surface-test.txt` appears in web Inbox
   - File metadata (name, size) matches Telegram upload
   - File can be downloaded from web UI

---

### 2. File Produced via Web PMA Sendable via Telegram

1. In the web PMA chat, ask the agent to write a file:
   ```
   Create web-produced.txt in the PMA outbox with "Cross-surface test from web".
   ```
2. **Expected:** File appears in web Outbox panel

3. In Telegram (same hub, PMA mode), check outbox:
   ```
   /files outbox
   ```
4. **Expected:** `web-produced.txt` is listed

5. Send the file:
   ```
   /files send web-produced.txt
   ```
6. **Expected:**
   - File is sent in the Telegram chat
   - File can be downloaded
   - Content matches what was created via web

---

### 3. Simultaneous Upload from Both Surfaces

1. **Web:** Upload `from-web.txt` via web UI
2. **Telegram:** Upload `from-telegram.txt` via Telegram (PMA mode)
3. **Expected:**
   - Both files appear in respective Inbox panels
   - Both files are accessible from both surfaces
   - Agent can reference both files

---

## Known Failure Modes

### File Too Large

**Symptom:** Upload fails with "File too large" error

**Causes:**
- Web: File exceeds `pma.max_upload_bytes` (default 10 MB)
- Telegram: File exceeds `telegram.media.max_file_bytes` (default 100 MB)

**Resolution:**
- Reduce file size or increase limits in config
- Config settings:
  - `pma.max_upload_bytes: <bytes>`
  - `telegram.media.max_file_bytes: <bytes>`

---

### Invalid Filename

**Symptom:** Upload fails with "Invalid filename" error

**Causes:**
- Path traversal attempts (e.g., `../evil.txt`, `..`, `a/b`)
- Empty filename
- Filenames with special characters rejected by sanitization

**Resolution:**
- Use simple filenames (alphanumeric, hyphens, underscores, dots)
- Avoid path separators and special characters

---

### Missing Hub Root

**Symptom:**
- Web: PMA routes return 404 or "PMA disabled"
- Telegram: Bot reports "PMA unavailable; hub root not configured"

**Causes:**
- Hub root not set in bot config
- Hub root path doesn't exist

**Resolution:**
- Configure `hub_root` in Telegram bot settings
- Verify hub root path exists and is accessible

---

### PMA Disabled

**Symptom:** PMA features unavailable

**Causes:**
- `pma.enabled: false` in hub config
- Bot topic not in PMA mode

**Resolution:**
- Set `pma.enabled: true` in `codex-autorunner.yml`
- Enable PMA mode in Telegram with `/pma on`

---

### Permissions Issues

**Symptom:** Upload or write fails with "Failed to save file"

**Causes:**
- Insufficient permissions on `.codex-autorunner/pma/` directory
- Disk full
- Filesystem read-only

**Resolution:**
- Check permissions: `ls -la <hub_root>/.codex-autorunner/pma/`
- Ensure write permissions for the bot/server user
- Verify available disk space

---

### Network Issues (Telegram)

**Symptom:**
- Bot reports "Failed to download file" or "Telegram file download failed"
- File doesn't appear in inbox after upload

**Causes:**
- Telegram API connectivity issues
- Bot rate limiting
- Temporary network outage

**Resolution:**
- Check bot connectivity to Telegram API
- Wait and retry (transient failures)
- Check Telegram bot logs for error details

---

### File Already Exists

**Symptom:** Agent writes fail to overwrite existing file

**Note:** This is expected behavior - the agent should use unique filenames.

**Resolution:**
- Agent should use unique names (e.g., with timestamps)
- Delete existing files before writing new ones
- Clear inbox/outbox if needed

---

### File Not Found (Download)

**Symptom:** Download returns 404 or "File not found"

**Causes:**
- File was deleted
- Filename was modified
- Wrong box specified (inbox vs outbox)

**Resolution:**
- Refresh file listing
- Verify file still exists in expected location
- Check filename for typos or encoding issues

---

### Missing Thread ID (Telegram)

**Symptom:** `/files send` fails or doesn't work as expected

**Causes:**
- Topic not properly initialized with PMA thread

**Resolution:**
- Re-enable PMA mode with `/pma on`
- Check bot logs for thread registry errors

---

## Troubleshooting Checklist

When file transfer fails:

1. **Check PMA is enabled:**
   ```bash
   # Web: Check config
   grep "pma.enabled" <hub_root>/codex-autorunner.yml

   # Telegram: Ask bot
   /pma status
   ```

2. **Verify directories exist:**
   ```bash
   ls -la <hub_root>/.codex-autorunner/pma/
   ls -la <hub_root>/.codex-autorunner/pma/inbox/
   ls -la <hub_root>/.codex-autorunner/pma/outbox/
   ```

3. **Check file sizes:**
   - Ensure files are under configured limits
   - Compare file size to `max_upload_bytes` / `max_file_bytes`

4. **Review logs:**
   - Web: Check hub server logs
   - Telegram: Check bot logs in `codex-autorunner.log`

5. **Test API endpoints:**
   ```bash
   # List files
   curl -H "Authorization: Bearer <token>" \
     http://<hub-root>/hub/pma/files

   # Upload test file
   curl -X POST \
     -F "file=@test.txt" \
     -H "Authorization: Bearer <token>" \
     http://<hub-root>/hub/pma/files/inbox
   ```

6. **Verify permissions:**
   - Ensure bot/server user has write access to PMA directories
   - Check filesystem is not read-only

---

## Success Criteria

The PMA file transfer E2E verification is successful when:

### Web PMA
- [ ] File upload works (file appears in inbox)
- [ ] Agent can read uploaded file
- [ ] Agent can write file to outbox
- [ ] Outbox file is visible in UI
- [ ] File download works correctly
- [ ] File deletion works correctly

### Telegram PMA
- [ ] PMA mode can be enabled/disabled
- [ ] File upload lands in PMA inbox
- [ ] Agent can read uploaded file
- [ ] Agent can write file to outbox
- [ ] `/files outbox` shows outbox files
- [ ] `/files send <name>` sends file successfully
- [ ] `/files clear` clears directories correctly

### Cross-Surface
- [ ] Telegram-uploaded files visible in web UI
- [ ] Web-produced files sendable via Telegram
- [ ] Both surfaces can access same files

---

## Automation Notes

This runbook can be automated with:
- Web: HTTP client tests (see `tests/test_pma_routes.py`)
- Telegram: Bot integration tests (see `tests/test_telegram_files_pma.py`)
- File system: Direct checks on `.codex-autorunner/pma/` directories

Key test files for reference:
- `tests/test_pma_routes.py` - Web PMA route tests
- `tests/test_telegram_files_pma.py` - Telegram PMA file tests
- `tests/test_telegram_pma_routing.py` - PMA routing and context tests
