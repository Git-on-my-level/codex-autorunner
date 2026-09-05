import test from 'node:test';import assert from 'node:assert/strict';import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);const {analyze,runtimeImports,sourceFiles}=require('../../scripts/check-foundation.cjs');
test('architectural dependency boundaries hold in the actual source tree',()=>{assert.deepEqual(analyze(sourceFiles()),[])});
test('type-only imports do not couple the runtime',()=>{assert.deepEqual(runtimeImports('import type {X} from "ai"; import {type Y} from "./service.ts"; export type {Z} from "./other.ts";','test.ts'),[])});
for(const variant of ['import {runner} from "./leak.ts";', 'const x=require("./leak.ts");', 'const x=import("./leak.ts");'])test('boundary catches transitive model dependency: '+variant,()=>{
 const files=sourceFiles();files['src/attention/service.ts']=variant;files['src/attention/leak.ts']='import {generateText} from "ai";';assert.ok(analyze(files).some(v=>v.includes('service.ts -> src/attention/leak.ts -> ai')));
});
test('agent MCP cannot obtain setup/human credentials via an indirect import',()=>{const files=sourceFiles();files['src/attention/mcp.ts']='export {initialize} from "./setup.ts";';assert.ok(analyze(files).some(v=>v.includes('Agent clients and MCP')))});
test('decision presentation cannot pull in a state-mutating repository',()=>{const files=sourceFiles();files['src/surfaces/web/decision_views.tsx']='import {recordAnswer} from "../../attention/replies.ts";';assert.ok(analyze(files).some(v=>v.includes('pure projections')))});

test('extensionless intermediaries cannot hide a model dependency',()=>{const files=sourceFiles();files['src/attention/service.ts']='import "./bridge";';files['src/attention/bridge.ts']='import "openai";';assert.ok(analyze(files).some(v=>v.includes('src/attention/bridge.ts -> openai')))});
test('empty named import still has runtime side effects',()=>{assert.deepEqual(runtimeImports('import {} from "ai";','test.ts'),['ai'])});
