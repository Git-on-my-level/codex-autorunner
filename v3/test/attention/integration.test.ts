/** Real Bun/Zod/Hono integration tests. No live model, network provider, or Telegram account. */
import { afterEach, describe, expect, test } from "bun:test";
import { Hono } from "hono";
import { createHmac } from "node:crypto";
import { FakeChannel, FakeClock, memoryStore, testConfig } from "../fakes.ts";
import { buildDeps } from "../web/helpers.ts";
import { AttentionService } from "../../src/attention/service.ts";
import { DecisionPacket, type ClientIdentity } from "../../src/attention/contract.ts";
import { createAttentionApi, validateAttentionCredentials } from "../../src/attention/http.ts";
import { createWebUi } from "../../src/surfaces/web/index.ts";
import { createPreparationWorker } from "../../src/attention/triage.ts";
import type { LlmRunner, LlmTurnResult } from "../../src/ports.ts";

type AttentionView = ReturnType<AttentionService["view"]>;
type AttentionSummary = ReturnType<AttentionService["summary"]>;
type AttentionListResponse = { requests: (AttentionSummary & { packet?: unknown })[]; next_cursor: string | null };

const AGENT = "test-agent-one-credential-012345678901234567890";
const OTHER = "test-agent-two-credential-012345678901234567890";
const HUMAN = "test-human-credential-012345678901234567890123";
const owner: ClientIdentity = { workspaceId: "integration", clientId: "mac", host: "mac-a" };
const cleanup: (() => void)[] = [];
afterEach(() => { for (const clean of cleanup.splice(0).reverse()) clean(); });
function fixture() {
  const clock = new FakeClock(); const store = memoryStore(clock); cleanup.push(() => store.db.close());
  for (const [key, value] of Object.entries({ CAR_INTEGRATION_A: AGENT, CAR_INTEGRATION_B: OTHER })) {
    const old = process.env[key]; process.env[key] = value;
    cleanup.push(() => { if (old === undefined) delete process.env[key]; else process.env[key] = old; });
  }
  const config = testConfig({ http: { private_reads: true, ingest_tokens: { web: HUMAN } }, attention: {
    workspace_id: "integration", prepare_seconds: 10, clients: {
      mac: { token_env: "CAR_INTEGRATION_A", host: "mac-a" }, vm: { token_env: "CAR_INTEGRATION_B", host: "vm-a" },
    },
  } });
  const channel = new FakeChannel(); const service = new AttentionService(store, config, channel);
  const app = new Hono(); const api = createAttentionApi(service); app.route(api.path, api.app);
  const ui = createWebUi({ ...buildDeps({ store, config }), channel }, service); app.route(ui.path, ui.app);
  return { clock, store, config, service, app, channel };
}
const bare = () => DecisionPacket.parse({ goal: "Ship migration", blocker: "Compatibility undecided", question: "Preserve v1?" });
const complete = () => DecisionPacket.parse({ ...bare(), why_human: "This is a new breaking-change decision", attempts: ["Checked callers"], facts: [{ statement: "One external caller remains", source: "src/client.ts:10" }], impact: "Blocks release", recommendation: { answer: "Keep v1 this release", rationale: "Avoid breaking a known caller" }, options: [{ id: "keep", label: "Keep v1", answer: "Keep v1 this release", consequences: "One more compatibility release" }] });
const auth = (token = AGENT) => ({ authorization: `Bearer ${token}` });
const json = (body: unknown, token = AGENT) => ({ method: "POST", headers: { ...auth(token), "content-type": "application/json" }, body: JSON.stringify(body) });
const form = (values: Record<string, string>, headers: Record<string, string> = auth(HUMAN)) => ({ method: "POST", headers: { ...headers, "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(values).toString() });
const result = (args: Record<string, unknown>): LlmTurnResult => ({ toolCalls: [{ tool: "submit_preparation", args }], model: "test", tokensIn: 1, tokensOut: 1, costUsd: 0 });

describe("attention HTTP and human boundary", () => {
  test("structured agent request, stable replay, no agent-side approval endpoint", async () => {
    const f = fixture(); validateAttentionCredentials(f.config);
    expect((await f.app.request('/v1/attention/requests')).status).toBe(401);
    const response = await f.app.request('/v1/attention/requests', json({ idempotency_key: 'migration', packet: bare() }));
    expect(response.status).toBe(200); const request = await response.json() as AttentionView;
    expect(request.state).toBe('preparing'); expect(request.preparation.context_requests.length).toBeGreaterThan(0);
    const duplicate = await (await f.app.request('/v1/attention/requests', json({ idempotency_key: 'migration', packet: bare() }))).json() as AttentionView;
    expect(duplicate.id).toBe(request.id);
    expect((await f.app.request(`/v1/attention/requests/${request.id}`, { headers: auth(OTHER) })).status).toBe(404);
    expect((await f.app.request(`/v1/attention/requests/${request.id}/answer`, json({ text: 'yes' }))).status).toBe(404);
    expect((await f.app.request(`/ui/decisions/${request.id}/answer`, form({ expected_revision: '1', text: 'yes' }, auth()))).status).toBe(401);
  });
  test("strict schema, streamed size limit, bounded lists and credential separation", async () => {
    const f = fixture();
    expect((await f.app.request('/v1/attention/requests', json({ idempotency_key: 'one', workspace_id: 'other', packet: bare() }))).status).toBe(400);
    expect((await f.app.request('/v1/attention/requests', json({ idempotency_key: 'big', packet: { ...bare(), blocker: 'x'.repeat(70_000) } }))).status).toBe(413);
    f.service.raise(owner, 'one', complete());
    const listing = await (await f.app.request('/v1/attention/requests', { headers: auth() })).json() as AttentionListResponse;
    expect(listing.requests[0]!.question).toBe('Preserve v1?'); expect(listing.requests[0]!.packet).toBeUndefined();
    process.env.CAR_INTEGRATION_B = AGENT; expect(() => validateAttentionCredentials(f.config)).toThrow('distinct agent credential');
    process.env.CAR_INTEGRATION_B = OTHER; process.env.CAR_INTEGRATION_A = HUMAN;
    expect(() => validateAttentionCredentials(f.config)).toThrow('distinct agent credential');
    const unsafe = testConfig({ http: { ingest_tokens: { '*': HUMAN } } });
    expect(() => validateAttentionCredentials(unsafe)).toThrow('Separate human');
  });
  test("decision UI escapes source text; answer is durable but not yet received or resolved", async () => {
    const f = fixture(); const packet = complete(); packet.question = '<script>alert(1)</script>';
    const row = f.service.raise(owner, 'one', packet);
    expect((await f.app.request('/ui')).status).toBe(303);
    const response = await f.app.request('/ui', { headers: auth(HUMAN) }); const html = await response.text();
    expect(response.status).toBe(200); expect(html).toContain('Needs you'); expect(html).toContain('Source agent recommends');
    expect(html).toContain('&lt;script&gt;'); expect(html).not.toContain('<script>alert(1)</script>');
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect((await f.app.request(`/ui/decisions/${row.id}/answer`, form({ expected_revision: '2', option_id: 'keep' }))).status).toBe(409);
    expect((await f.app.request(`/ui/decisions/${row.id}/answer`, form({ expected_revision: '1', option_id: 'keep' }))).status).toBe(303);
    expect(f.service.get(row.id)?.state).toBe('answered');
    const read = await (await f.app.request(`/v1/attention/requests/${row.id}`, { headers: auth() })).json() as AttentionView;
    const answer = read.answer;
    expect(answer).not.toBeNull();
    if (!answer) throw new Error("Expected the recorded answer in the request view");
    expect(answer.payload.text).toBe('Keep v1 this release');
    expect(answer.delivery).toBe('staged'); expect(f.service.get(row.id)?.state).toBe('answered');
    const ack = await f.app.request(`/v1/attention/requests/${row.id}/ack`, json({ answer_id: answer.id, outcome: 'received' }));
    expect(ack.status).toBe(200); expect(f.service.get(row.id)?.state).toBe('received');
    const done = await f.app.request(`/v1/attention/requests/${row.id}/ack`, json({ answer_id: answer.id, outcome: 'resolved', note: 'Work resumed' }));
    expect(done.status).toBe(200); expect(f.service.get(row.id)?.state).toBe('resolved');
    expect(f.store.listGrants()).toHaveLength(0);
  });
  test("TLS proxy origin, cookie expiry and CSRF are enforced without forwarded-header trust", async () => {
    const f = fixture(); f.config.http.public_origin = 'https://car.example';
    const login = await f.app.request('http://internal/ui/login', form({ token: HUMAN }, { origin: 'https://car.example' }));
    expect(login.status).toBe(303); expect(login.headers.get('set-cookie')).toContain('Secure');
    expect(login.headers.get('referrer-policy')).toBe('same-origin');
    const cookie = login.headers.get('set-cookie')!.split(';')[0]!;
    expect((await f.app.request('http://internal/ui/logout', form({}, {
      cookie, origin: 'null', 'sec-fetch-site': 'same-origin',
    }))).status).toBe(401);
    expect((await f.app.request('http://internal/ui/logout', form({}, {
      cookie, origin: 'https://evil.example', 'sec-fetch-site': 'cross-site', 'x-forwarded-host': 'evil.example',
    }))).status).toBe(401);
    const expiry = Math.floor(Date.now()/1000) - 10;
    const signature = createHmac('sha256', HUMAN).update(`car-ui-v2:${expiry}`).digest('base64url');
    expect((await f.app.request('/ui', { headers: { cookie: `car_ui_session=v2.${expiry}.${signature}` } })).status).toBe(303);
    expect((await f.app.request('http://internal/ui/logout', form({}, {
      cookie, origin: 'https://car.example', 'sec-fetch-site': 'same-origin',
    }))).status).toBe(303);
  });
});

describe("optional preparation reviewer", () => {
  test("proposal is advisory, source-private and never a grant or answer", async () => {
    const f = fixture(); const other: ClientIdentity = { workspaceId: 'integration', clientId: 'vm', host: 'vm-a' };
    const old = f.service.raise(other, 'other-private', complete()); const answer = f.service.answer(old.id, 1, 'human:test', { text: 'OTHER CLIENT SECRET' });
    f.service.acknowledge(other, old.id, answer.id, 'received');
    f.service.acknowledge(other, old.id, answer.id, 'resolved');
    const row = f.service.raise(owner, 'one', bare()); let calls = 0;
    const runner: LlmRunner = { async turn(input) { calls++; expect(input.messages[0]!.content).not.toContain('OTHER CLIENT SECRET');
      return result({ context_requests: [{ instruction: 'Which callers still use v1?' }], uncertainty: ['External usage is unknown'] }); } };
    const worker = createPreparationWorker(f.service, runner); await worker.tick(); await worker.tick();
    expect(calls).toBe(1); expect(f.service.get(row.id)?.state).toBe('preparing');
    expect(f.store.listGrants()).toHaveLength(0);
    expect(f.store.db.query('SELECT * FROM human_replies WHERE request_id=?').get(row.id)).toBeNull();
    f.clock.advance(11_000); f.service.sweep(); expect(f.service.get(row.id)?.state).toBe('needs_you');
  });
  test("late reviewer output cannot hide or revise an already surfaced request", async () => {
    const f = fixture(); const row = f.service.raise(owner, 'one', bare());
    let finish!: (r: LlmTurnResult) => void;
    const runner: LlmRunner = { turn: () => new Promise((resolve) => { finish = resolve; }) };
    const worker = createPreparationWorker(f.service, runner); const run = worker.tick();
    f.clock.advance(11_000); f.service.sweep();
    finish(result({ context_requests: [], uncertainty: [] })); await run;
    expect(f.service.get(row.id)?.state).toBe('needs_you');
    expect(f.store.db.query('SELECT state FROM attention_triage_runs WHERE request_id=?').get(row.id)).toMatchObject({ state: 'failed' });
  });
});

describe("pre-release foundation boundaries", () => {
  test("expired decisions require human review; agent cannot review or withdraw another source", async () => {
    const f = fixture(); const row = f.service.raise(owner, 'missed', { ...complete(), deadline_at: new Date(f.clock.now().getTime()+1_000).toISOString() });
    f.clock.advance(1_000);
    const home = await f.app.request('/ui', { headers: auth(HUMAN) });
    expect(await home.text()).toContain('Acknowledge missed decision');
    expect((await f.app.request(`/ui/decisions/${row.id}/review-expiry`, form({expected_revision:'1',note:'Reviewed'},auth()))).status).toBe(401);
    expect((await f.app.request(`/ui/decisions/${row.id}/review-expiry`, form({expected_revision:'1',note:'Asked for a new request'}))).status).toBe(303);
    expect(f.service.get(row.id)?.state).toBe('expired'); expect(f.service.get(row.id)?.reviewed_at).not.toBeNull();
  });
  test("stale form retains text without echoing executable markup or claiming it was recorded", async () => {
    const f = fixture(); const row = f.service.raise(owner, 'one', complete());
    const response = await f.app.request(`/ui/decisions/${row.id}/answer`, form({expected_revision:'999',text:'<script>draft</script>'}));
    expect(response.status).toBe(409);const html=await response.text();
    expect(html).toContain('Action not confirmed');expect(html).toContain('&lt;script&gt;draft&lt;/script&gt;');expect(html).not.toContain('<script>draft');
    expect(response.headers.get('cache-control')).toBe('no-store');
  });
  test("real Hono render includes exact answer and consequences without trusting source HTML", async () => {
    const f=fixture();const row=f.service.raise(owner,'view',{...complete(),question:'<img src=x onerror=alert(1)>'});
    const html=await (await f.app.request(`/ui/decisions/${row.id}`,{headers:auth(HUMAN)})).text();
    expect(html).toContain('Keep v1 this release');expect(html).toContain('One more compatibility release');
    expect(html).toContain('&lt;img');expect(html).not.toContain('<img src=x');
  });
  test("preparation stop aborts the actual runner and cannot suppress a request", async () => {
    const f=fixture();const row=f.service.raise(owner,'one',bare());let signal:AbortSignal|undefined;
    const runner:LlmRunner={turn(input){signal=input.signal;return new Promise(()=>{});}};
    const worker=createPreparationWorker(f.service,runner);const run=worker.tick();await worker.stop();await run;
    expect(signal?.aborted).toBe(true);expect(f.service.get(row.id)?.state).toBe('preparing');
    f.clock.advance(11_000);f.service.sweep();expect(f.service.get(row.id)?.state).toBe('needs_you');
  });
  test("late reviewer result is rejected even when the publication sweep has not run",async()=>{
    const f=fixture();const row=f.service.raise(owner,'one',bare());let finish!:(v:LlmTurnResult)=>void;
    const worker=createPreparationWorker(f.service,{turn:()=>new Promise(resolve=>{finish=resolve;})});const run=worker.tick();
    f.clock.advance(11_000);finish(result({context_requests:[],uncertainty:[]}));await run;
    expect(f.store.db.query('SELECT state FROM attention_triage_runs WHERE request_id=?').get(row.id)).toMatchObject({state:'failed'});
  });
});

describe("one authoritative agent schema", () => {
  test("MCP parser rejects invalid fields, missing packets and raw receipt shortcuts", async () => {
    const {parseMcpArguments}=await import('../../src/attention/contract.ts');
    expect(()=>parseMcpArguments('car_raise',{idempotency_key:'one',packet:{goal:'Only a goal'}})).toThrow();
    expect(()=>parseMcpArguments('car_get',{id:'req_one',workspace_id:'another'})).toThrow();
    expect(()=>parseMcpArguments('car_ack',{id:'req_one',answer_id:'reply_one',outcome:'received'})).toThrow();
    expect(parseMcpArguments('car_raise',{idempotency_key:'one',packet:bare()})).toMatchObject({contract:'car.request.v1'});
    expect(parseMcpArguments('car_guide',{})).toEqual({});
  });
});
