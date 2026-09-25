/** Test-only renderer for synchronous, pure JSX views when Hono is unavailable.
 * Not a Hono substitute: it cannot test routing, middleware or framework escaping.
 * Bun tests exercise the same views with real Hono. Browser fixtures name this seam.
 */
const escape = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
class Markup { constructor(value) { this.value=value } toString() { return this.value } }
const render = value => value instanceof Markup ? value.value : Array.isArray(value) ? value.map(render).join('') : value===null||value===undefined||typeof value==='boolean' ? '' : escape(value);
export const Fragment = Symbol('Fragment');
const voidTags = new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
export function jsx(type, props={}) {
 if(typeof type==='function') return type(props);
 if(type===Fragment) return new Markup(render(props.children));
 let attrs='';for(const [key,value] of Object.entries(props)) {
   if(['children','dangerouslySetInnerHTML','key','ref'].includes(key)||value===null||value===undefined||value===false)continue;
   const name=key==='className'?'class':key==='charSet'?'charset':key==='htmlFor'?'for':key;
   attrs+=value===true?` ${name}`:` ${name}="${escape(value)}"`;
 }
 const body=props.dangerouslySetInnerHTML?.__html ?? render(props.children);
 return new Markup(`<${type}${attrs}>${voidTags.has(type)?'':`${body}</${type}>`}`);
}
export const jsxs=jsx, jsxDEV=jsx;
