# Agent Cheat Sheet (token-minimal)

Default v3 posture: **provider proposals require core grants and safety**. Repo
development work follows the user's task authorization. YOLO is a deprecated v2 runtime
mode, not a v3 effect policy.

1) Identify your context (repo-dev vs runtime task).
2) Load the current durable source of truth (core state decides; provider memory is context).
3) In unfamiliar CLI areas, run `card <group> --help` for v3; use `car <group> --help`
   only when explicitly maintaining deprecated v2.
4) Inspect current/recent runs/logs to avoid duplicate work.
5) Choose the smallest change that achieves the goal.
6) Act (execute, edit, run commands) without ceremony.
7) Validate with concrete evidence (tests/logs/artifacts).
8) Persist outcomes, authority, and delivery evidence.
9) Exit with a crisp summary and pointers to evidence.
