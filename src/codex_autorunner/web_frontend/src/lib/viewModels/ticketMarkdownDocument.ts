/** Parse and compose CAR ticket markdown files (YAML frontmatter + body). */

export type TicketMarkdownFields = {
  title: string;
  agent: string;
  model: string;
  reasoning: string;
  done: boolean;
  frontmatterYaml: string;
  body: string;
};

export type TicketPackListRow = {
  numberLabel: string;
  title: string;
  pathLabel: string;
  agentLabel: string;
  modelLabel: string | null;
  bodyPreview: string | null;
};

export function extractFrontmatterYamlFromMarkdown(markdown: string): string | null {
  const text = markdown.replace(/\r\n/g, '\n');
  if (!text.startsWith('---\n')) return null;
  const rest = text.slice(4);
  const end = rest.indexOf('\n---\n');
  if (end !== -1) return rest.slice(0, end);
  const endAlt = rest.indexOf('\n---');
  if (endAlt === -1) return null;
  return rest.slice(0, endAlt);
}

export function markdownBodyWithoutFrontmatter(markdown: string): string {
  const text = markdown.replace(/\r\n/g, '\n');
  if (!text.startsWith('---\n')) return text.trimEnd();
  const rest = text.slice(4);
  const end = rest.indexOf('\n---\n');
  if (end !== -1) return rest.slice(end + 5).trimStart();
  const endAlt = rest.indexOf('\n---');
  if (endAlt === -1) return text.trimEnd();
  return rest.slice(endAlt + 4).trimStart();
}

export function parseYamlScalarFromBlock(block: string | null, key: string): string | null {
  if (!block || !/^[a-zA-Z0-9_]+$/.test(key)) return null;
  for (const line of block.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const match = trimmed.match(new RegExp(`^${key}\\s*:\\s*(.*)$`));
    if (!match) continue;
    let value = match[1].trim();
    if (!value) return null;
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    return value || null;
  }
  return null;
}

export function parseYamlBooleanFromBlock(block: string | null, key: string): boolean {
  const raw = parseYamlScalarFromBlock(block, key);
  if (!raw) return false;
  const normalized = raw.trim().toLowerCase();
  return ['1', 'true', 'yes'].includes(normalized);
}

export function parseTicketMarkdownFields(content: string, path = ''): TicketMarkdownFields {
  const frontmatterYaml = extractFrontmatterYamlFromMarkdown(content) ?? '';
  const body = markdownBodyWithoutFrontmatter(content);
  const titleFromYaml = parseYamlScalarFromBlock(frontmatterYaml, 'title');
  const titleFromPath =
    path
      .split('/')
      .pop()
      ?.replace(/\.md$/i, '')
      .replace(/[-_]+/g, ' ')
      .trim() || '';
  const title = titleFromYaml ?? (titleFromPath || 'Untitled ticket');
  return {
    title,
    agent: parseYamlScalarFromBlock(frontmatterYaml, 'agent') ?? 'codex',
    model: parseYamlScalarFromBlock(frontmatterYaml, 'model') ?? '',
    reasoning: parseYamlScalarFromBlock(frontmatterYaml, 'reasoning') ?? '',
    done: parseYamlBooleanFromBlock(frontmatterYaml, 'done'),
    frontmatterYaml,
    body
  };
}

export function upsertYamlScalar(block: string, key: string, value: string | boolean | null): string {
  const lines = block.replace(/\r\n/g, '\n').split('\n');
  const keyPattern = new RegExp(`^${key}\\s*:`);
  const replacement = value === null ? null : `${key}: ${yamlScalar(value)}`;
  const nextLines: string[] = [];
  let replaced = false;
  for (const line of lines) {
    if (keyPattern.test(line)) {
      replaced = true;
      if (replacement !== null) nextLines.push(replacement);
    } else {
      nextLines.push(line);
    }
  }
  if (!replaced && replacement !== null) nextLines.push(replacement);
  return nextLines.join('\n').trimEnd();
}

export function composeTicketMarkdown(payload: {
  title: string;
  agent: string;
  model: string;
  reasoning: string;
  done: boolean;
  frontmatterYaml?: string;
  body: string;
}): string {
  let frontmatterYaml = (payload.frontmatterYaml ?? '').trim();
  if (frontmatterYaml) {
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'title', payload.title.trim() || 'Untitled ticket');
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'agent', payload.agent.trim() || 'codex');
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'done', payload.done);
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'model', payload.model.trim() || null);
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'reasoning', payload.reasoning.trim() || null);
  } else {
    const frontmatter: Record<string, unknown> = {
      title: payload.title.trim() || 'Untitled ticket',
      agent: payload.agent.trim() || 'codex',
      done: payload.done
    };
    if (payload.model.trim()) frontmatter.model = payload.model.trim();
    if (payload.reasoning.trim()) frontmatter.reasoning = payload.reasoning.trim();
    frontmatterYaml = serializeFrontmatter(frontmatter).trimEnd();
  }
  return `---\n${frontmatterYaml}\n---\n\n${payload.body.trimEnd()}\n`;
}

export function ticketPackNumberLabel(path: string, index: number): string {
  const base = path.split('/').pop()?.replace(/\.md$/i, '') ?? '';
  const match = base.match(/(\d+)/);
  return match ? `#${Number(match[1])}` : `#${index + 1}`;
}

export function ticketPackListRow(path: string, content: string, index: number): TicketPackListRow {
  const fields = parseTicketMarkdownFields(content, path);
  const preview = fields.body
    .replace(/\r\n/g, '\n')
    .split('\n')
    .map((line) => line.trim())
    .find((line) => line.length > 0);
  return {
    numberLabel: ticketPackNumberLabel(path, index),
    title: fields.title,
    pathLabel: path,
    agentLabel: fields.agent,
    modelLabel: fields.model || null,
    bodyPreview: preview ?? null
  };
}

export function defaultTicketPackTicketContent(path: string): string {
  const fields = parseTicketMarkdownFields('', path);
  return composeTicketMarkdown({
    ...fields,
    title: fields.title,
    agent: 'codex',
    done: false,
    body: ''
  });
}

function serializeFrontmatter(frontmatter: Record<string, unknown>): string {
  const preferred = ['agent', 'done', 'ticket_id', 'title', 'goal', 'profile', 'model', 'reasoning'];
  const keys = [
    ...preferred.filter((key) => Object.prototype.hasOwnProperty.call(frontmatter, key)),
    ...Object.keys(frontmatter).filter((key) => !preferred.includes(key)).sort()
  ];
  return keys.map((key) => `${key}: ${yamlScalar(frontmatter[key])}\n`).join('');
}

function yamlScalar(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (value === null || value === undefined) return 'null';
  if (Array.isArray(value) || typeof value === 'object') return JSON.stringify(value);
  return JSON.stringify(String(value));
}
