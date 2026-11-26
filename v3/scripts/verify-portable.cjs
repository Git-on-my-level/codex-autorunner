#!/usr/bin/env node
/**
 * Supplemental verification when Bun/dependencies are unavailable.
 * Runs real Store/attention/client code on Node's SQLite with a narrow Bun API
 * shim. It does NOT replace `bun test` or dependency-aware TypeScript checking.
 */
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const { spawnSync } = require('node:child_process');
const ts = require(process.env.CAR_TYPESCRIPT_PATH || 'typescript');
const root = path.resolve(__dirname, '..');
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'car-portable-'));
const compilerOptions = { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  jsx: ts.JsxEmit.ReactJSX, jsxImportSource: 'hono/jsx', esModuleInterop: true };
let syntaxErrors = 0, sourceFiles = 0;
function emit(source, filename) {
  const result = ts.transpileModule(source, { fileName: filename, compilerOptions, reportDiagnostics: true });
  for (const diagnostic of result.diagnostics || []) if (diagnostic.category === ts.DiagnosticCategory.Error) {
    console.error(filename + ': ' + ts.flattenDiagnosticMessageText(diagnostic.messageText, ' ')); syntaxErrors++;
  }
  return result.outputText.replace(/(from\s+["'][^"']+)\.tsx?(["'])/g, '$1.js$2').replace(/(import\(["'][^"']+)\.tsx?(["']\))/g, '$1.js$2');
}
function compile(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) compile(file);
    else if (/\.tsx?$/.test(file) && !file.endsWith('.d.ts')) {
      sourceFiles++;
      let code = emit(fs.readFileSync(file, 'utf8'), file);
      if (file.endsWith('/store/db.ts')) code = code.replace('"bun:sqlite"', '"../../shims/sqlite.mjs"').replace('"../contract/events.js"', '"../contract/event-helpers.js"').replace('"../contract/lifecycle.js"', '"../contract/lifecycle-helpers.js"');
      const destination = path.join(output, path.relative(root, file)).replace(/\.tsx?$/, '.js');
      fs.mkdirSync(path.dirname(destination), { recursive: true }); fs.writeFileSync(destination, code);
    }
  }
}
// Extract the exact dependency-free helpers from contract source. Do not
// duplicate their logic or use a substitute Store in the tests.
function extract(file, names, destination) {
  const text = fs.readFileSync(file, 'utf8'); const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const statements = source.statements.filter((statement) =>
    (ts.isFunctionDeclaration(statement) && names.includes(statement.name?.text)) ||
    (ts.isVariableStatement(statement) && statement.declarationList.declarations.some((d) => ts.isIdentifier(d.name) && names.includes(d.name.text))));
  fs.writeFileSync(path.join(output, destination), emit(statements.map((s) => s.getText(source)).join('\n'), destination.replace(/\.js$/, '.ts')));
}
try {
  compile(path.join(root, 'src')); compile(path.join(root, 'test'));
  console.log(`TypeScript syntax: ${sourceFiles} files; ${syntaxErrors} errors (transpile only, not type checking).`);
  if (syntaxErrors) process.exitCode = 1;
  else {
    fs.writeFileSync(path.join(output, 'package.json'), '{"type":"module"}');
    fs.mkdirSync(path.join(output, 'shims'));
    const jsxDir=path.join(output,'node_modules/hono'); fs.mkdirSync(jsxDir,{recursive:true});
    fs.writeFileSync(path.join(jsxDir,'package.json'),JSON.stringify({name:'hono',type:'module',exports:{'./jsx/jsx-runtime':'./portable-jsx.mjs'}}));
    fs.copyFileSync(path.join(root,'test/support/portable-jsx.mjs'),path.join(jsxDir,'portable-jsx.mjs'));

    fs.writeFileSync(path.join(output, 'shims/sqlite.mjs'), `
      import { DatabaseSync } from 'node:sqlite';
      export class Database {
        constructor(file) { this.db=new DatabaseSync(file); this.depth=0; }
        exec(sql) { return this.db.exec(sql); }
        query(sql) { const s=this.db.prepare(sql); return { get:(...args)=>s.get(...args) ?? null, all:(...args)=>s.all(...args), run:(...args)=>s.run(...args) }; }
        transaction(fn) { return (...args)=>{ const n=this.depth++; const name='car_'+n; this.db.exec(n ? 'SAVEPOINT '+name : 'BEGIN IMMEDIATE'); try { const value=fn(...args); this.db.exec(n ? 'RELEASE SAVEPOINT '+name : 'COMMIT'); return value; } catch(error) { this.db.exec(n ? 'ROLLBACK TO SAVEPOINT '+name : 'ROLLBACK'); if(n) this.db.exec('RELEASE SAVEPOINT '+name); throw error; } finally { this.depth--; } }; }
        close() { this.db.close(); }
      }
    `);
    fs.writeFileSync(path.join(output, 'shims/bootstrap.mjs'), `import { createHash } from 'node:crypto'; globalThis.Bun={CryptoHasher:class { constructor(algorithm){this.hash=createHash(algorithm)} update(value){this.hash.update(value);return this} digest(format){return this.hash.digest(format)} }};`);
    extract(path.join(root,'src/contract/events.ts'), ['HASH_SEP','sessionKey'], 'src/contract/event-helpers.js');
    extract(path.join(root,'src/contract/lifecycle.ts'), ['AGENT_RUN_SUCCESS_STATES','AGENT_RUN_FAILURE_STATES','AGENT_RUN_TERMINAL_STATES','agentRunSuccessStates','agentRunFailureStates','agentRunTerminalStates','normalizeAgentRunState','isAgentRunSuccessState','isAgentRunFailureState','isAgentRunTerminalState'], 'src/contract/lifecycle-helpers.js');
    const tests = fs.readdirSync(path.join(root,'test/portable')).filter((name)=>name.endsWith('.case.mjs')).map((name)=>path.join(root,'test/portable',name));
    const result=spawnSync(process.execPath,['--import',path.join(output,'shims/bootstrap.mjs'),'--test',...tests],{stdio:'inherit',env:{...process.env,CAR_PORTABLE_ROOT:output}});
    process.exitCode=result.status ?? 1;
  }
} finally { if (!process.env.CAR_KEEP_PORTABLE) fs.rmSync(output,{recursive:true,force:true}); else console.log(`Portable build: ${output}`); }
