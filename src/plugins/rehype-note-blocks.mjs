// Shapes Markdown output like the hand-written pages: ```text / ```diagram
// fences become <pre class="diagram">, other code fences get a copy button,
// and tables get a scroll wrapper.

const DIAGRAM_LANGS = new Set(["text", "diagram"]);

function element(tagName, properties, children) {
  return { type: "element", tagName, properties, children };
}

function codeLang(pre) {
  const code = pre.children.find((c) => c.type === "element" && c.tagName === "code");
  if (!code) return null;
  const cls = (code.properties.className || []).find((c) => String(c).startsWith("language-"));
  return { code, lang: cls ? String(cls).slice("language-".length) : "" };
}

function transform(parent) {
  parent.children = parent.children.map((node) => {
    if (node.type !== "element") return node;

    if (node.tagName === "pre") {
      const info = codeLang(node);
      if (!info) return node;
      const last = info.code.children.at(-1);
      if (last?.type === "text") last.value = last.value.replace(/\n$/, "");
      if (DIAGRAM_LANGS.has(info.lang)) {
        node.properties.className = ["diagram"];
        info.code.properties.className = undefined;
        return node;
      }
      info.code.properties.className = info.lang ? [`lang-${info.lang}`] : undefined;
      const button = element(
        "button",
        { type: "button", className: ["copy-btn"], ariaLabel: "Copy code" },
        [{ type: "text", value: "Copy" }],
      );
      return element("div", { className: ["code-block"] }, [button, node]);
    }

    if (node.tagName === "table") {
      return element("div", { className: ["table-wrap"] }, [node]);
    }

    transform(node);
    return node;
  });
}

export default function rehypeNoteBlocks() {
  return (tree) => transform(tree);
}
