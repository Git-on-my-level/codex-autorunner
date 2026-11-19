# Native team dogfood — 2026-09-05

## Verdict and scope

**Initial run blocked; see full-access retry below for subsequent native evidence.**
Do not count this as replay, enrichment, supersession, receipt, or resolution acceptance.
Checkout: `ec2c44a5`, isolated server `127.0.0.1:7197`; user preview `7194`
remained unchanged and returned HTTP 200. No product implementation changes.

Evidence directory: `/tmp/car-team-dogfood.gF8dDQ/` (private local artifacts;
connection credentials must not be committed). Scenario and role reports are there.

## Native executions

| Role | Requested model | agentctl execution |
|---|---|---|
| Scenario author | Cursor cursor-grok-4.6-high | exec-write-trial-boost-victory-protect-eternal |
| Investigator | Cursor cursor-grok-4.6-high | exec-torch-street-stay-fat-budget-sad |
| Release conductor | Cursor cursor-grok-4.6-high | exec-pole-spawn-miracle-scatter-spice-ten |
| Compatibility auditor | OMP glm-5.3 | exec-erosion-obey-monkey-excite-legend-panel |
| Hotfix applicator | OMP glm-5.3 | exec-figure-case-skirt-define-unit-million |

Models followed live agentctl advisory configuration, not inferred defaults.
Each participant had its own agent connection; only coordinator used human UI.
Grok authored a Northstar Billing canary-release scenario before participants ran.
Coordinator narrowed a proposed factual root-cause question into a human risk
decision. Scenario author also supplied fixtures/packets; participants subsequently
authored role-specific packets. This is a synthetic incident, not production evidence.

## What actually happened

- Grok investigated fixture code/config/logs and wrote a grounded findings note.
- OMP authored a complete compatibility question, validated its packet offline,
  and prepared an apply-validation script. No post-answer fixture work completed.
- Investigator reported commands rejected before execution, including Bun and
  localhost WebFetch. Compatibility agent reported unattended tool approval
  failures, filesystem EPERM and blocked network in its alternate tool environment.
  These are harness-environment reports, not CAR protocol failures.
- Coordinator independently reproduced a CLI startup defect from repo root:
  `bun run v3/src/cli.ts request list` fails resolving `react/jsx-dev-runtime` from
  web layout. The same operation from `v3/` succeeds. Absolute entrypoint paths do
  not remove working-directory dependence.
- Coordinator submitted OMP's frozen compatibility packet using its designated
  profile from `v3/`. Server accepted request
  `req_5e27d178e3403851f3036bbe4b95c39f31d7` as `needs_you`.
- In the real browser, R opened custom reply and Ctrl+Enter submitted. The reply
  preserved v1 for 3.4.0 but explicitly rejected automatic removal in 3.5.0.
  Needs you stayed selected, with Reply recorded and an empty inbox.
- SQLite showed `answered`, not received/resolved. Watching correctly displayed
  Awaiting receipt and Saved — receipt not confirmed. No completion was fabricated.
- Disabled letter/number shortcuts, navigated to Watching, reopened help:
  the checkbox was checked again. Preference does not survive navigation.

## Improvements, ranked

1. **Runnable agent connection preflight.** Verify exact harness can execute the
   CLI, write its scoped spool, authenticate to the endpoint, and persist receipt.
   A model READY response and harness installed status are insufficient. Surface a
   narrowly scoped operator remedy; never silently bypass sandbox/approval rules.
2. **Working-directory-independent client.** Decouple agent CLI/MCP startup from
   server/web imports; test absolute-path invocation from another project directory.
3. **Compact question versus full authority text.** The live 1280x720 screenshot
   showed a long multi-line heading and repeated rationale pushing actions below
   the viewport. Encourage short titles/labels, collapse secondary explanation,
   and retain exact answer/conditions visibly at selection time. Do not silently
   summarize away commitments: the suggested preserve option also proposed a
   future removal horizon the human did not accept.
4. **Show the actual answer first in Watching.** The old recommendation and its
   impact/uncertainty still precede the recorded reply. Put reply plus receipt
   state near the top; keep original reasoning in the history below.
5. **Persist shortcut preference.** Navigation must not re-enable shortcuts after
   the human disabled them. Also distinguish keyboard preference from dirty reply
   state and test it across all mailbox routes.
6. **Make conditions easy to add.** This exercise needed “preserve, but no future
   removal commitment.” A selected option plus explicit conditions/composed final
   reply would be easier than starting a custom answer from scratch. Submission
   must show the exact resulting answer and retain one-request scope.

## Harness limitations and rerun gate

The scenario author terminalized but agentctl could not retrieve stored final
content; the written scenario remains evidence. Background cancellation for the
remaining runs returned native cancellation route not verified; they have explicit
18-minute execution deadlines. Do not use raw process control to bypass that owner.

Before rerunning, provide a reviewed, session-scoped execution path for Bun plus
the isolated state/spool and localhost endpoint (or a supported agent-only MCP
connection). Do not globally enable unrestricted tool execution. Then repeat the
four lifecycle probes with actual source receipt and validated post-answer work.

## Full-access retry (explicitly authorized by user)

The user subsequently authorized full agent access without sandboxing. Per-run
native flags were verified from CLI help: Cursor `--force --sandbox disabled`
plus `--trust`; OMP `--approval-mode yolo` using native bash. No global settings
were changed. All prior executions had ended before the retry.

Two Cursor Grok 4.6 roles and two OMP GLM-5.3 roles reused the synthetic scenario,
with new request keys. Separate bounded native phases cover submission and
post-human-answer work; coordinator operates only the human UI. Unlike the first
run, coordinator did not submit these requests for the agents.

| Probe / role | Request | Verified evidence |
|---|---|---|
| Incomplete context / Grok | req_7e28a2522c43ba306869701c54c370b70630 | Raw raise `preparing` revision 1; enrich `needs_you` revision 2, same ID; native receipt, answer note, resolved |
| Replay / OMP SDK | req_11e1cfe94a6baacc210b89b48e1e2a738a02 | Two raw raises same ID/revision; one obligation; native receive, actual compatibility doc edits, resolved |
| Supersession / Grok | req_f4736ebca79b9bc507d4e29d3cf6279f8298 → req_225a91b289bd11a12452b5b9e1121b0929fb | Old cancelled with reason, original packet unchanged, can_apply_answer false; new answered, received, RELEASE.md and answer note written, resolved |
| Unnecessary hold / OMP ops | req_0062a441f3ed5c6fde33dbfdf085dab881b4 | Agent asked permission to wait despite overriding task instructions; coordinator withdrew it; cancelled, not successful |
| Final apply / OMP ops | req_7a44a0a35292fa01d860da8c9b13520388ce | Grounded request after prerequisite notes; human answered; pre-receipt resolve rejected; native receipt, three actual file changes, independent checks, resolved |

Raw retry evidence is in `retry-spool/<role>/`; role reports are
`<role>-retry.md` in the evidence directory. Native execution IDs:

- Submit Grok data: `exec-label-cereal-stone-panther-spatial-dose`
- Submit Grok release: `exec-risk-kangaroo-chuckle-spy-embody-idle`
- Submit OMP SDK: `exec-ticket-legal-camera-flip-face-garage`
- OMP ops initial: `exec-emotion-breeze-bronze-tip-dismiss-job`
- Receive OMP SDK: `exec-earth-message-word-spoon-pole-chuckle`
- Receive Grok release: `exec-nature-custom-debate-family-noise-cotton`
- Receive Grok data: `exec-focus-legend-arrow-cute-diagram-crater`
- OMP final apply request: `exec-tunnel-word-gas-slender-toy-ranch`
- OMP final execution: `exec-survey-image-barrel-book-legend-begin`

### Further findings from real work

- **Confirmation attribution:** after withdrawing the hold question, the next
  active question displayed “Request withdrawn” above it. Reload retained the
  notice through query parameters. Name/link the previous request so the next
  item's state cannot be mistaken for the completed action.
- **Agent-generated obligations:** ops treated older scenario instructions as
  overriding the specific retry instruction not to raise a hold question. CAR
  accepted its complete packet correctly, but this creates avoidable human work.
  Guidance should distinguish dependency waiting from a genuine authority gap.
- **Stale context:** final ops packet said the 21:00 ET freeze window was open
  after 22:27 ET and said external systems were unreachable despite full-access
  launch. These are source-content inaccuracies, not proof of router failure.
  Keep simulated context/time and “not authorized” distinct from observed reality.
- **Receipt snapshot labeling:** independently inspected the SDK disk receipt:
  it still says `answered`, delivery `staged`, can_apply_answer false. CLI receive
  response correctly says `received`/acknowledged/true. The client deliberately
  writes before acknowledging; label this immutable snapshot as historical and
  require fresh server state, rather than suggesting it is current workflow guidance.
- **Actual custom-answer fidelity passed:** SDK did not copy its suggested 3.5.0
  removal commitment. Both artifacts explicitly preserved the human's separate
  post-migration-decision requirement and no-contact constraint.
- **Do not trust generated validators blindly:** the ops script asserts pre-apply
  values during post mode and parses natural-language decisions case-sensitively.
  It can fail a correct apply and skip checks on unparsed wording. Final acceptance
  requires independent semantic/TOML checks, with its failure recorded separately.

No product implementation changes were made in this assessment.

### Final retry acceptance

**Passed the exercised native CLI-to-human-UI-to-native-work workflow.** All four
substantive retry requests resolved; the superseded gate and redundant hold request
remained cancelled. The original coordinator-assisted request from the blocked run
remained answered/unreceived, intentionally untouched. Browser final counts:
Needs you 0, Watching 1 (that old request), Handled 6 (four resolved, two cancelled).

OMP's pre-receipt resolution probe returned `receipt_required`; subsequent GET
still showed answered. Receive then returned received. The final config changed
only payments_v2 to false, retaining adapter v2, sandbox merchant ID, and 5%
traffic. Changelog documented no automatic 3.5.0 removal; partner notice was an
unsent local draft. The primary independently parsed TOML and asserted these
conditions, then checked unchanged release/compatibility hashes. OMP's additional
20 assertions passed. Its original validator remained exit 1 with the three known
flaws, preserved in raw evidence rather than concealed.

Evidence: `retry-spool/omp-ops/22-probe-ack-before-receive.*`,
`23-get-after-probe.json`, `24-receive.json`, `26-validate-independent.txt`,
`27-validate-old-script-post.txt`, `29-fixtures-diff.txt`, `30-ack-resolved.json`.
Database independently confirmed final state resolved.

This does not establish restart/offline replay, MCP transport, external delivery,
or production safety. These were bounded local fixture tasks, with coordinator
dispatching explicit phases and answering human questions. Full native tool access
did not remove CAR's agent/human separation or receipt-before-resolution guard.
