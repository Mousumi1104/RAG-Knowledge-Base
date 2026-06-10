"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS = [rehypeKatex];

/**
 * Render assistant message text as GitHub-flavoured Markdown with LaTeX math.
 *
 * Supports tables/lists (GFM), inline (`$...$`) and block (`$$...$$`) math and
 * sub/superscripts via KaTeX. react-markdown tolerates incomplete input, so it
 * is safe to render the partial text that arrives while a turn is streaming.
 * Links open in a new tab. The KaTeX stylesheet is imported in the root layout.
 */
export default function Markdown({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={{
          a: ({ ...props }) => <a target="_blank" rel="noopener noreferrer" {...props} />,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
