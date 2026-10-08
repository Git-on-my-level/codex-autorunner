#!/usr/bin/env node
/** Small architectural tripwire. Tests still establish behavior; this only fences dependencies. */
const fs = require('node:fs'), path = require('node:path');
const ts = require(process.env.CAR_TYPESCRIPT_PATH || 'typescript');
const root = path.resolve(__dirname, '..');
function runtimeImports(text, filename) {
  const source=ts.createSourceFile(filename,text,ts.ScriptTarget.Latest,true,filename.endsWith('.tsx')?ts.ScriptKind.TSX:ts.ScriptKind.TS);
  const found=[];
  function visit(node) {
    if(ts.isImportDeclaration(node)) {
      const c=node.importClause;
      if(c?.isTypeOnly)return;
      if(c&&!c.name&&c.namedBindings&&ts.isNamedImports(c.namedBindings)&&c.namedBindings.elements.length>0&&c.namedBindings.elements.every(e=>e.isTypeOnly))return;
      if(ts.isStringLiteral(node.moduleSpecifier))found.push(node.moduleSpecifier.text);
    } else if(ts.isExportDeclaration(node)) {
      if(node.isTypeOnly)return;
      if(node.exportClause&&ts.isNamedExports(node.exportClause)&&node.exportClause.elements.length>0&&node.exportClause.elements.every(e=>e.isTypeOnly))return;
      if(node.moduleSpecifier&&ts.isStringLiteral(node.moduleSpecifier))found.push(node.moduleSpecifier.text);
    } else if(ts.isCallExpression(node)&&(node.expression.kind===ts.SyntaxKind.ImportKeyword||(ts.isIdentifier(node.expression)&&node.expression.text==='require'))) {
      if(node.arguments[0]&&ts.isStringLiteral(node.arguments[0]))found.push(node.arguments[0].text);
      else found.push('<dynamic-module>'); // Dynamic wiring belongs at the composition root.
    }
    ts.forEachChild(node,visit);
  }
  visit(source);return found;
}
const modelModule = value => value==='ai'||value==='openai'||value.startsWith('@anthropic-ai/')||value.startsWith('@ai-sdk/')||/^src\/(providers|attention\/triage\.ts|triage\/llm\.ts)/.test(value)||value==='<dynamic-module>';
const serverModule = value => /^src\/(store|router|providers|safety|effects|ingest|surfaces|config)\//.test(value)||/^src\/attention\/(service|replies|setup|http|triage)\.ts$/.test(value)||modelModule(value);
const rules=[
 {name:'Deterministic request state cannot depend on a model/provider',entries:['src/attention/service.ts','src/attention/replies.ts','src/attention/guidance.ts','src/store/db.ts'],forbidden:modelModule},
 {name:'Human decision views are pure projections, never state owners',entries:['src/surfaces/web/decision_views.tsx','src/surfaces/web/layout.tsx'],forbidden:v=>/^src\/(store|router|providers|safety|effects|config|ingest)\//.test(v)||/^src\/attention\/(service|replies|http|setup|triage)\.ts$/.test(v)||modelModule(v)},
 {name:'Agent clients and MCP cannot import human/server authority',entries:['src/attention/client.ts','src/attention/mcp.ts'],forbidden:serverModule},
];
function analyze(files) {
  const errors=[];
  function resolve(file,spec) {
    if(!spec.startsWith('.'))return spec;
    const target=path.posix.normalize(path.posix.join(path.posix.dirname(file),spec));
    const candidates=[target,`${target}.ts`,`${target}.tsx`,`${target}/index.ts`,`${target}/index.tsx`,target.replace(/\.js$/,'.ts'),target.replace(/\.js$/,'.tsx')];
    return candidates.find(candidate=>Object.hasOwn(files,candidate)) ?? target;
  }
  const edges=new Map(Object.entries(files).map(([file,text])=>[file,runtimeImports(text,file).map(spec=>resolve(file,spec))]));
  for(const rule of rules)for(const entry of rule.entries){
    if(!edges.has(entry)){errors.push(`${rule.name}: missing entry ${entry}`);continue;}
    const seen=new Set();
    function visit(file,trail){if(seen.has(file))return;seen.add(file);for(const next of edges.get(file)||[]){
      if(rule.forbidden(next))errors.push(`${rule.name}: ${[...trail,next].join(' -> ')}`);
      else if(edges.has(next))visit(next,[...trail,next]);
    }}
    visit(entry,[entry]);
  }
  return errors;
}
function sourceFiles(dir=path.join(root,'src'), result={}) {
 for(const e of fs.readdirSync(dir,{withFileTypes:true})){const f=path.join(dir,e.name);if(e.isDirectory())sourceFiles(f,result);else if(/\.tsx?$/.test(e.name))result[path.relative(root,f).split(path.sep).join('/')]=fs.readFileSync(f,'utf8');}
 return result;
}
module.exports={analyze,runtimeImports,sourceFiles};
if(require.main===module){
 const errors=analyze(sourceFiles());
 if(errors.length){console.error(errors.join('\n'));process.exitCode=1;}else console.log('Foundation dependency boundaries: passed (deterministic core, pure decision views, thin agent adapters).');
}
