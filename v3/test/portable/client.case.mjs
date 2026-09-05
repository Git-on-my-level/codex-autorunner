import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, rmSync, readdirSync, readFileSync, statSync, chmodSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
const load=(p)=>import(pathToFileURL(join(process.env.CAR_PORTABLE_ROOT,'src',p+'.js')));
const { AttentionClient, checkedServerUrl }=await load('attention/client');
const { createMcpHandler }=await load('attention/mcp');
const { writePrivateJson, readPrivateJson }=await load('attention/files');
const packet={goal:'Ship',blocker:'Need judgment',question:'Keep?',attempts:[],facts:[],options:[],uncertainty:[],urgency:'normal'};
const view=(overrides={})=>({contract:'car.request.v1',id:'req_one',revision:1,state:'needs_you',answer:null,next_action:'Poll',...overrides});
async function fixture(t,handler) {
 const dir=mkdtempSync(join(tmpdir(),'car-client-')); const server=createServer(handler);
 await new Promise((resolve)=>server.listen(0,'127.0.0.1',resolve));
 t.after(async()=>{server.closeAllConnections();await new Promise((resolve)=>server.close(resolve));rmSync(dir,{recursive:true,force:true})});
 const url=`http://127.0.0.1:${server.address().port}`;
 return {server,dir,url,client:new AttentionClient({url,token:'a'.repeat(40),spoolDir:dir,timeoutMs:150})};
}
const json=(res,data,status=200)=>{res.writeHead(status,{'content-type':'application/json'});res.end(JSON.stringify(data))};
const pendingFiles=(dir)=>readdirSync(dir,{recursive:true}).filter((n)=>n.includes('pending/')&&n.endsWith('.json'));
test('remote TLS is the default and URL credentials are rejected',()=>{assert.throws(()=>checkedServerUrl('http://remote.example'),{code:'insecure_transport'});assert.equal(checkedServerUrl('http://host.tailnet',true),'http://host.tailnet');assert.throws(()=>checkedServerUrl('https://user:secret@host.example'),{code:'invalid_url'});assert.throws(()=>checkedServerUrl('https://host.example/path'),{code:'invalid_url'});assert.equal(checkedServerUrl('http://127.0.0.1:7171'),'http://127.0.0.1:7171')});
test('request is on disk before transport sees it, then deleted after server acceptance',async t=>{let seen=false;let dir;const f=await fixture(t,(req,res)=>{assert.equal(pendingFiles(dir).length,1);assert.equal(req.headers.authorization,'Bearer '+'a'.repeat(40));seen=true;json(res,view())});dir=f.dir;const result=await f.client.raise('stable',packet);assert.equal(result.id,'req_one');assert.equal(seen,true);assert.equal(pendingFiles(f.dir).length,0)});
test('ambiguous first transport attempt retains the exact request for replay',async t=>{let attempt=0;let bodies=[];const f=await fixture(t,(req,res)=>{let text='';req.on('data',c=>text+=c);req.on('end',()=>{bodies.push(JSON.parse(text));if(++attempt===1)req.socket.destroy();else json(res,view())})});const result=await f.client.raise('stable',packet);assert.equal(result.delivery,'accepted_locally');assert.equal(pendingFiles(f.dir).length,1);const replay=await f.client.flush();assert.equal(replay.accepted.length,1);assert.deepEqual(bodies[0],bodies[1]);assert.equal(pendingFiles(f.dir).length,0)});
test('an unrecognized successful response is not accepted as durable confirmation',async t=>{const f=await fixture(t,(_,res)=>json(res,{ok:true}));const result=await f.client.raise('stable',packet);assert.equal(result.delivery,'accepted_locally');assert.equal(pendingFiles(f.dir).length,1)});
test('spool never persists raw authentication tokens',async t=>{const f=await fixture(t,(_,res)=>json(res,{error:'unavailable'},503));const result=await f.client.raise('stable',packet);const text=readFileSync(result.spool_file,'utf8');assert.equal(text.includes('a'.repeat(40)),false);assert.equal(statSync(result.spool_file).mode&0o077,0)});
test('pending key cannot be overwritten with different content',async t=>{const f=await fixture(t,(_,res)=>json(res,{error:'unavailable'},503));await f.client.raise('stable',packet);await assert.rejects(()=>f.client.raise('stable',{...packet,question:'Other?'}),{code:'idempotency_conflict'})});
test('wrong endpoint or rotated credential cannot silently inherit pending requests',async t=>{const f=await fixture(t,(_,res)=>json(res,{error:'unavailable'},503));await f.client.raise('stable',packet);const second=new AttentionClient({url:f.url,token:'b'.repeat(40),spoolDir:f.dir});assert.notEqual(second.audience,f.client.audience);assert.equal((await second.flush()).accepted.length,0);assert.equal(pendingFiles(f.dir).length,1)});
test('client refuses redirect chains instead of forwarding a bearer credential',async t=>{let redirected=0;const f=await fixture(t,(req,res)=>{if(req.url==='/elsewhere'){redirected++;json(res,view())}else{res.writeHead(302,{location:'/elsewhere'});res.end()}});const result=await f.client.raise('stable',packet);assert.equal(result.delivery,'accepted_locally');assert.equal(redirected,0)});
test('receiving an answer writes a local receipt before acknowledgement',async t=>{let dir;let acked=0;const answer={id:'reply_one',payload:{text:'Keep'},eligible_for_receipt:true,delivery:'staged'};const f=await fixture(t,(req,res)=>{if(req.method==='GET')json(res,view({state:'answered',answer}));else {const files=readdirSync(dir,{recursive:true}).filter(n=>n.includes('answers/')&&n.endsWith('.json'));assert.equal(files.length,1);acked++;json(res,view({state:'received',answer:{...answer,delivery:'acknowledged'}}))}});dir=f.dir;const result=await f.client.receive('req_one');assert.equal(acked,1);assert.equal(result.state,'received')});
test('get and wait do not claim an answer was received',async t=>{let posts=0;const answer={id:'reply_one',payload:{text:'Keep'},eligible_for_receipt:true,delivery:'staged'};const f=await fixture(t,(req,res)=>{if(req.method==='POST')posts++;json(res,view({state:'answered',answer}))});await f.client.get('req_one');await f.client.wait('req_one',0);assert.equal(posts,0)});
test('expired answers are not acknowledged or treated as fresh permission',async t=>{let posts=0;const f=await fixture(t,(req,res)=>{if(req.method==='POST')posts++;json(res,view({state:'expired',answer:{id:'reply_old',payload:{text:'Keep'},eligible_for_receipt:false,delivery:'expired'}}))});assert.equal((await f.client.receive('req_one')).state,'expired');assert.equal(posts,0)});
test('prepare instructions return immediately rather than waiting silently',async t=>{const f=await fixture(t,(_,res)=>json(res,view({state:'preparing'})));assert.equal((await f.client.wait('req_one',3600)).state,'preparing')});
test('private JSON creation is no-clobber and rejects readable secret files',t=>{const dir=mkdtempSync(join(tmpdir(),'car-files-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));const path=join(dir,'test.json');assert.equal(writePrivateJson(path,{one:1}),true);assert.equal(writePrivateJson(path,{two:2}),false);assert.deepEqual(readPrivateJson(path),{one:1});chmodSync(path,0o644);assert.throws(()=>readPrivateJson(path),/chmod 600/)});
const schemas={raise:{type:'object',properties:{},required:[]},context:{type:'object',properties:{},required:[]},ack:{type:'object',properties:{},required:[]},cancel:{type:'object',properties:{},required:[]}};
const validate=(_tool,args)=>args;
const rpc=(id,method,params)=>({jsonrpc:'2.0',id,method,params});
test('MCP negotiates version and requires initialization notification',async()=>{const handle=createMcpHandler({},schemas,validate);assert.equal((await handle(rpc(1,'tools/list'))).error.code,-32002);const initialized=await handle(rpc(2,'initialize',{protocolVersion:'future'}));assert.equal(initialized.result.protocolVersion,'2025-11-25');assert.equal((await handle(rpc(3,'tools/list'))).error.code,-32002);assert.equal(await handle({jsonrpc:'2.0',method:'notifications/initialized'}),null);const tools=(await handle(rpc(4,'tools/list'))).result.tools;assert.equal(tools.length,9);assert.equal(tools.some(t=>/approve|answer|grant|setup/.test(t.name)),false)});
test('MCP delegates to the ordinary client, returning structured tool errors',async()=>{let calls=[];const handle=createMcpHandler({get:async(id)=>{calls.push(id);return view()},raise:async()=>{throw Object.assign(new Error('key reused'),{code:'idempotency_conflict'})}},schemas,validate);await handle(rpc(1,'initialize',{protocolVersion:'2025-11-25'}));await handle({jsonrpc:'2.0',method:'notifications/initialized'});assert.equal((await handle(rpc(2,'tools/call',{name:'car_get',arguments:{id:'req_one'}}))).result.isError,false);assert.deepEqual(calls,['req_one']);assert.equal((await handle(rpc(3,'tools/call',{name:'car_raise',arguments:{}}))).result.isError,true);assert.equal((await handle(rpc(4,'tools/call',{name:'grant',arguments:{}}))).error.code,-32602)});
test('MCP rejects malformed JSON-RPC and does not answer notifications',async()=>{const h=createMcpHandler({},schemas,validate);assert.equal((await h([])).error.code,-32600);assert.equal((await h({jsonrpc:'2.0',id:null,method:'ping'})).error.code,-32600);assert.equal(await h({jsonrpc:'2.0',method:'ping'}),null)});

test('definite HTML validation failures are quarantined rather than reported as offline success',async t=>{
 const f=await fixture(t,(_,res)=>{res.writeHead(400,{'content-type':'text/html'});res.end('<h1>Bad request</h1>')});
 await assert.rejects(()=>f.client.raise('invalid',packet),{code:'http_error',status:400,retryable:false});
 assert.equal(pendingFiles(f.dir).length,0);assert.equal(readdirSync(f.dir,{recursive:true}).filter(n=>n.includes('rejected/')&&n.endsWith('.json')).length,1);
});
test('auth failures preserve the request for credential repair without draining the queue',async t=>{
 let online=false;const f=await fixture(t,(_,res)=>{if(!online)json(res,{error:'unavailable'},503);else{res.writeHead(401);res.end('Unauthorized')}});
 await f.client.raise('one',packet);await f.client.raise('two',packet);online=true;
 const result=await f.client.flush();assert.equal(result.pending.length,1);assert.equal(result.remaining,2);assert.equal(result.accepted.length,0);
});
test('a rejected poison request does not prevent another request from reaching the server',async t=>{
 let online=false;const f=await fixture(t,(req,res)=>{let body='';req.on('data',v=>body+=v);req.on('end',()=>{const input=JSON.parse(body);if(!online)json(res,{},503);else if(input.idempotency_key==='bad')json(res,{error:'invalid_request'},400);else json(res,view())})});
 await f.client.raise('bad',packet);await f.client.raise('good',packet);online=true;
 const result=await f.client.flush();assert.equal(result.rejected.length,1);assert.equal(result.accepted.length,1);assert.equal(result.remaining,0);assert.equal('delivered' in result,false);
});
test('response identities are checked before a receipt is written',async t=>{
 const f=await fixture(t,(_,res)=>json(res,view({id:'req_another',state:'answered',answer:{id:'a',payload:{text:'Keep'},eligible_for_receipt:true,delivery:'staged'}})));
 await assert.rejects(()=>f.client.receive('req_one'),{code:'invalid_response'});assert.equal(readdirSync(f.dir,{recursive:true}).filter(n=>n.includes('answers/')&&n.endsWith('.json')).length,0);
});
test('a corrupt local answer receipt is not overwritten or acknowledged',async t=>{
 let answer='Keep',posts=0;const f=await fixture(t,(req,res)=>{if(req.method==='POST')posts++;json(res,view({state:req.method==='POST'?'received':'answered',answer:{id:'reply_one',payload:{text:answer},eligible_for_receipt:true,delivery:req.method==='POST'?'acknowledged':'staged'}}))});
 await f.client.receive('req_one');answer='Delete everything';await assert.rejects(()=>f.client.receive('req_one'),{code:'receipt_conflict'});assert.equal(posts,1);
});
test('MCP invokes the required validator before accepting offline work',async()=>{
 let calls=0;const handle=createMcpHandler({raise:async()=>{calls++;return {delivery:'accepted_locally'}}},schemas,()=>{throw new Error('packet missing required fields')});
 await handle(rpc(1,'initialize',{protocolVersion:'2025-11-25'}));await handle({jsonrpc:'2.0',method:'notifications/initialized'});
 const response=await handle(rpc(2,'tools/call',{name:'car_raise',arguments:{}}));assert.equal(response.result.isError,true);assert.equal(calls,0);
});
test('MCP cancellation suppresses a late response without pretending the remote write was undone',async()=>{
 let finish;const handle=createMcpHandler({raise:()=>new Promise(resolve=>{finish=resolve})},schemas,validate);
 await handle(rpc(1,'initialize',{protocolVersion:'2025-11-25'}));await handle({jsonrpc:'2.0',method:'notifications/initialized'});
 const running=handle(rpc(2,'tools/call',{name:'car_raise',arguments:{idempotency_key:'one',packet}}));
 assert.equal((await handle(rpc(2,'tools/call',{name:'car_raise',arguments:{idempotency_key:'one',packet}}))).error.code,-32600);
 await handle({jsonrpc:'2.0',method:'notifications/cancelled',params:{requestId:2}});finish(view());assert.equal(await running,null);
 const guide=await handle(rpc(3,'tools/call',{name:'car_guide',arguments:{}}));assert.equal(guide.result.structuredContent.contract,'car.guide.v1');
});
