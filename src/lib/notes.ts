import type { MarkdownInstance } from "astro";

// A note is src/content/<group>/<slug>.md (Markdown) or .html (a ready-made
// chat fragment). Both start with frontmatter: `title`, optional `order`
// (pinned notes come first, in that order) and, for Markdown, optional
// `question` to show a "You" bubble above the answer.

interface Frontmatter {
  title: string;
  order?: number;
  question?: string;
}

export interface Note extends Frontmatter {
  group: string;
  slug: string;
  html?: string;
  Content?: MarkdownInstance<Frontmatter>["Content"];
}

const markdown = import.meta.glob<MarkdownInstance<Frontmatter>>("/src/content/*/*.md", { eager: true });
const fragments = import.meta.glob<string>("/src/content/*/*.html", {
  query: "?raw",
  import: "default",
  eager: true,
});

const FRONTMATTER_RE = /^---\n([\s\S]*?)\n---\n/;

function parseFragment(raw: string): { data: Frontmatter; html: string } {
  const match = raw.match(FRONTMATTER_RE);
  const data: Record<string, unknown> = {};
  for (const line of match ? match[1].split("\n") : []) {
    const sep = line.indexOf(":");
    if (sep === -1) continue;
    const value = line.slice(sep + 1).trim();
    try {
      data[line.slice(0, sep).trim()] = JSON.parse(value);
    } catch {
      data[line.slice(0, sep).trim()] = value;
    }
  }
  return { data: data as unknown as Frontmatter, html: match ? raw.slice(match[0].length) : raw };
}

function pathParts(path: string) {
  const [group, file] = path.split("/").slice(-2);
  return { group, slug: file.replace(/\.(md|html)$/, "") };
}

function byOrderThenTitle(a: Note, b: Note) {
  const pa = a.order ?? Infinity;
  const pb = b.order ?? Infinity;
  if (pa !== pb) return pa - pb;
  return a.title.localeCompare(b.title, undefined, { sensitivity: "base" });
}

const notes: Note[] = [
  ...Object.entries(markdown).map(([path, mod]) => ({
    ...pathParts(path),
    ...mod.frontmatter,
    Content: mod.Content,
  })),
  ...Object.entries(fragments).map(([path, raw]) => {
    const { data, html } = parseFragment(raw);
    return { ...pathParts(path), ...data, html };
  }),
].sort(byOrderThenTitle);

export function getNotes(): Note[] {
  return notes;
}

export function getNote(group: string, slug: string): Note | undefined {
  return notes.find((n) => n.group === group && n.slug === slug);
}

export function getNav(): [string, Note[]][] {
  const groups = new Map<string, Note[]>();
  for (const note of notes) {
    if (!groups.has(note.group)) groups.set(note.group, []);
    groups.get(note.group)!.push(note);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}
