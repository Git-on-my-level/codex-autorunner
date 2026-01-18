# Security Fix Summary - Command Injection Vulnerabilities

This document summarizes all 6 command injection vulnerabilities that were fixed.

## 1. routes/shared.py:53-98 - Command injection in terminal commands

**Vulnerability:** Model and reasoning parameters from user input were passed to `apply_codex_options()` without validation, allowing potential command injection.

**Fix Location:** `src/codex_autorunner/routes/shared.py`

**Changes:**
- Added `_validate_model_parameter()` function with regex pattern to validate model names
- Added `_validate_reasoning_parameter()` function with regex pattern to validate reasoning parameters
- Modified `build_codex_terminal_cmd()` to validate model/reasoning before use
- Modified `build_opencode_terminal_cmd()` to validate model before use

**Validation Patterns:**
- Model: `^[a-zA-Z0-9._-]+$`
- Reasoning: `^[a-zA-Z0-9._:-]+$`

## 2. core/utils.py:89-110 - Path traversal & command injection in executable resolution

**Vulnerability:** `resolve_executable()` accepted arbitrary paths like `../../../malicious.sh` without validating they're within allowed directories.

**Fix Location:** `src/codex_autorunner/core/utils.py`

**Changes:**
- Added `_SAFE_EXECUTABLE_PATTERN` regex to validate simple binary names
- Added `_is_safe_path()` function to check if paths are within allowed directories
- Modified `resolve_executable()` to:
  - Validate path is within allowed directories (home/.opencode/bin, /opt/homebrew/bin, /usr/local/bin, /usr/bin, etc.)
  - Validate binary names against safe pattern
  - Return None for any unsafe path

## 3. core/update.py:20-36 - Subprocess command injection

**Vulnerability:** `_run_cmd()` and update functions executed commands without validating repo URLs, allowing arbitrary repository cloning.

**Fix Location:** `src/codex_autorunner/core/update.py`

**Changes:**
- Added `_REPO_URL_PATTERN` regex to restrict to github.com URLs
- Added `_validate_repo_url()` function to validate repository URLs
- Modified `_run_cmd()` to validate all command arguments
  - Check for null bytes
  - Validate argument types
- Added `_validate_repo_url()` calls in:
  - `_system_update_worker()`
  - `_system_update_check()`
  - `_spawn_update_process()`

**Validation Pattern:**
- `^https://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+\.git$|^https://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$`

## 4. core/git_utils.py:40-61 - Git command injection

**Vulnerability:** `run_git()` and related functions concatenated arguments without sanitization, allowing git ref injection.

**Fix Location:** `src/codex_autorunner/core/git_utils.py`

**Changes:**
- Added `_GIT_REF_PATTERN` regex to validate git references
- Added `_validate_git_ref()` function to validate refs
- Modified `run_git()` to validate arguments
  - Check for null bytes
  - Validate argument types
- Modified `git_diff_name_status()` to validate `from_ref` and `to_ref`

**Validation Pattern:**
- `^[a-zA-Z0-9._/@#-]+$`

## 5. integrations/github/service.py:105-172 - Shell command injection via user input

**Vulnerability:** GitHub API commands used owner/repo names from user input without validation, allowing potential injection.

**Fix Location:** `src/codex_autorunner/integrations/github/service.py`

**Changes:**
- Added `_GITHUB_OWNER_REPO_PATTERN` regex to validate owner/repo names
- Added `_GITHUB_REF_PATTERN` regex to validate GitHub refs
- Added `_validate_github_owner_repo()` function to validate owner/repo names
- Added `_validate_github_ref()` function to validate refs
- Modified functions to validate user input:
  - `parse_issue_input()` - validates owner/repo from URL
  - `parse_pr_input()` - validates owner/repo from URL
  - `ensure_pr_head()` - validates branch names
  - `issue_meta()` - validates owner/repo before API call
  - `issue_comments()` - validates owner/repo before API call
  - `create_issue_comment()` - validates owner/repo before API call
  - `pr_review_threads()` - validates owner/repo before API call

**Validation Patterns:**
- Owner/Repo: `^[a-zA-Z0-9_.-]+$`
- Ref: `^[a-zA-Z0-9_/#-]+$`

## 6. integrations/telegram/handlers/messages.py:192-202 - Arbitrary shell command execution

**Vulnerability:** `!` prefix bypassed normal processing, passing input directly to `bash -lc`, allowing arbitrary shell command execution.

**Fix Location:** `src/codex_autorunner/integrations/telegram/handlers/messages.py` and `src/codex_autorunner/integrations/telegram/handlers/commands_runtime.py`

**Changes:**
- Added `SAFE_COMMAND_ALLOWLIST` with safe commands: `ls`, `pwd`, `cat`, `head`, `tail`, `grep`, `git`, `echo`, `which`, `type`, `test`, `[[`
- Added `_COMMAND_PATTERN` regex to validate command syntax
- Added `_validate_shell_command()` function to:
  - Parse command with `shlex.split()`
  - Validate base command against allowlist
  - Check for null bytes
  - Check for invalid characters
- Modified `_handle_bang_shell()` in `commands_runtime.py` to:
  - Validate commands before execution
  - Execute with list args instead of `bash -lc command_text`
  - Display helpful error messages with list of safe commands
  - Remove shell invocation, pass command directly

**Validation Pattern:**
- `^[a-zA-Z0-9_\-\s/\.]+$`

## Summary of All Changes

| File | Lines Changed | Vulnerability Fixed |
|------|---------------|-------------------|
| `routes/shared.py` | 18-98 | Model/reasoning parameter validation |
| `core/utils.py` | 1-117 | Path traversal prevention |
| `core/update.py` | 1-562 | Repo URL validation |
| `core/git_utils.py` | 1-153 | Git ref validation |
| `integrations/github/service.py` | 1-1157 | GitHub owner/repo validation |
| `integrations/telegram/handlers/messages.py` | 1-616 | Command allowlist setup |
| `integrations/telegram/handlers/commands_runtime.py` | 1-6090 | Shell command validation |

All changes use:
- `subprocess.run()` with list arguments (never `shell=True`)
- Input validation with regex patterns
- Allowlist-based command restriction
- Null byte detection
- Type checking for arguments
