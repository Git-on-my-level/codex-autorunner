# Error Troubleshooting Guide

This guide helps diagnose and resolve common errors encountered when using codex-autorunner.

## Error Categories

Errors are categorized by their recoverability:

- **Transient Errors**: Temporary failures (network, rate limits) that auto-retry
- **Permanent Errors**: Configuration or authentication issues requiring manual fix
- **Critical Errors**: System failures requiring intervention

## Common Error Patterns

### Transient Errors (Auto-Retry)

#### Rate Limited
**Message:** `Telegram API is rate limited` or `OpenAI rate limited the request`

**Cause:** Exceeded API rate limits for Telegram or OpenAI.

**Solution:** Wait and retry. The system automatically retries with exponential backoff.

**Prevention:**
- Reduce polling frequency in Telegram config
- Use a paid OpenAI plan with higher rate limits

#### Network Timeout
**Message:** `Connection timeout` or `Request timed out`

**Cause:** Network connectivity issues or slow response from external services.

**Solution:** Check network connectivity. Auto-retry should handle temporary issues.

**Prevention:**
- Configure longer timeouts in config
- Check firewall/network settings

#### App-Server Disconnected
**Message:** `App-server temporarily unavailable. Reconnecting...`

**Cause:** The codex app-server process crashed or was killed.

**Solution:** System auto-restarts with exponential backoff. Check logs for root cause.

**Prevention:**
- Ensure sufficient system resources (RAM, CPU)
- Check for app-server version compatibility

### Permanent Errors (Manual Fix Required)

#### Configuration Error
**Message:** `ConfigError: Invalid path` or `ConfigError: Missing required field`

**Cause:** Misconfigured YAML files or invalid paths.

**Solution:**
1. Run `car doctor` to validate configuration
2. Check `codex-autorunner.yml` and `codex-autorunner.override.yml`
3. Ensure all paths are absolute or relative to repository root

**Prevention:**
- Use `car doctor` after config changes
- Validate YAML syntax before committing

#### Voice Authentication Error
**Message:** `Voice transcription failed: Invalid API key. Please set OPENAI_API_KEY.`

**Cause:** Missing or invalid OpenAI API key for voice transcription.

**Solution:**
1. Set environment variable: `export OPENAI_API_KEY=sk-...`
2. Or configure in YAML with `voice.providers.openai_whisper.api_key_env`
3. Verify API key has Whisper API access

**Prevention:**
- Store API keys in environment variables
- Rotate keys periodically

#### Voice Invalid Audio
**Message:** `Voice transcription failed: Invalid audio. Try re-recording.`

**Cause:** Audio format not supported or corrupted recording.

**Solution:**
1. Re-record the audio clip
2. Try different browser if using web UI
3. Ensure microphone permissions are granted

#### Voice Audio Too Large
**Message:** `Voice transcription failed: Audio too large. Record a shorter clip.`

**Cause:** Audio exceeded size limits (typically 25MB for OpenAI).

**Solution:**
1. Record shorter clips (under 30 seconds recommended)
2. Configure `voice.push_to_talk.max_ms` to enforce max duration

### Critical Errors (System-Level)

#### Circuit Breaker Open
**Message:** `[SERVICE_NAME] is temporarily unavailable. Please try again later.`

**Cause:** Circuit breaker opened due to repeated failures from an external service.

**Solution:**
1. Wait for circuit timeout (default: 60 seconds)
2. Check service status (Telegram status page, OpenAI status dashboard)
3. Review logs for root cause of repeated failures

**Prevention:**
- Monitor error rates for external services
- Implement proper error handling in custom integrations

## Debugging Steps

### 1. Enable Verbose Logging

```bash
# Set log level to DEBUG
export CAR_LOG_LEVEL=DEBUG
car run
```

### 2. Check Logs

```bash
# View main log
tail -f .codex-autorunner/codex-autorunner.log

# View app-server log (if using hub)
tail -f .codex-autorunner/codex-server.log
```

### 3. Run Diagnostics

```bash
# Validate configuration
car doctor

# Check system health
car status

# View recent errors
car log --level error --tail 50
```

### 4. Check for Circuit Breaker State

Circuit breakers log state transitions:

```
WARNING Circuit breaker OPEN for Telegram after 5 failures
INFO Circuit breaker HALF_OPEN for Telegram (testing recovery)
INFO Circuit breaker CLOSED for Telegram (recovery successful)
```

## Error Recovery Hints

Error messages include actionable guidance where possible:

- **Transient**: "Retrying with backoff..."
- **Permanent**: "Check configuration" or "Verify credentials"
- **Critical**: "Please try again later" or "Contact support"

## Best Practices

1. **Don't ignore warnings**: Often precede permanent failures
2. **Monitor error rates**: Use `car log --level error` to track patterns
3. **Test configuration changes**: Run `car doctor` after config updates
4. **Secure credentials**: Use environment variables for API keys
5. **Handle transient errors**: Let auto-retry do its work before manual intervention

## Getting Help

If issues persist:

1. Check the [GitHub Issues](https://github.com/Git-on-my-level/codex-autorunner/issues) for known problems
2. Search logs for specific error messages
3. Create a new issue with:
   - Error message
   - Steps to reproduce
   - Relevant log snippets (sanitize sensitive data)
   - Configuration details (remove secrets)
