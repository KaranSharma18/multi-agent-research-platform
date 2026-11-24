import { Fragment, type ReactNode } from "react";
import type { Citation, Final } from "../api";

// A deliberately small markdown renderer: paragraphs, bullet lists, bold,
// inline code and [ev_x] citation chips. Text is never injected as HTML, so an
// answer quoting a malicious document cannot run script in the page.
function inline(text: string, cites: Map<string, Citation>): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[ev_[A-Za-z0-9_]+(?:,\s*ev_[A-Za-z0-9_]+)*\])/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    if (/^\[ev_/.test(part)) {
      const ids = part.slice(1, -1).split(/,\s*/);
      return (
        <Fragment key={i}>
          {ids.map((id) => (
            <a key={id} className={`cite ${cites.get(id)?.flagged ? "flagged" : ""}`} href={`#${id}`}
               title={cites.get(id)?.title ?? id}>
              {id.replace("ev_", "")}
            </a>
          ))}
        </Fragment>
      );
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

export function Markdown({ text, citations }: { text: string; citations: Citation[] }) {
  const cites = new Map(citations.map((c) => [c.id, c]));
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) {
      blocks.push(<ul key={blocks.length}>{list.map((item, i) => <li key={i}>{inline(item, cites)}</li>)}</ul>);
      list = [];
    }
  };
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (bullet) {
      list.push(bullet[1]);
      continue;
    }
    flush();
    if (!line.trim()) continue;
    if (/^calculation:/i.test(line.trim())) blocks.push(<p key={blocks.length} className="calc">{inline(line, cites)}</p>);
    else if (/^#{1,3}\s/.test(line)) blocks.push(<h4 key={blocks.length}>{line.replace(/^#+\s*/, "")}</h4>);
    else blocks.push(<p key={blocks.length}>{inline(line, cites)}</p>);
  }
  flush();
  return <div className="markdown">{blocks}</div>;
}

export function AnswerCard({ final }: { final: Final }) {
  return (
    <section className="card answer">
      <header>
        <h3>Answer</h3>
        <span className={`pill ${final.verification?.passed ? "ok" : final.verification ? "warn" : ""}`}>
          {final.verification ? (final.verification.passed ? "verified" : "check flagged issues") : final.status}
        </span>
      </header>
      <Markdown text={final.answer} citations={final.citations} />
      {final.notes.length > 0 && (
        <div className="notes">{final.notes.map((n, i) => <p key={i}>{n}</p>)}</div>
      )}
      {final.assumptions.length > 0 && (
        <p className="muted">Assumed: {final.assumptions.join("; ")}</p>
      )}
      {final.citations.length > 0 && (
        <ol className="sources">
          {final.citations.map((c) => (
            <li key={c.id} id={c.id}>
              <span className="source-kind">{c.source}</span> <strong>{c.title}</strong>
              {c.flagged && <span className="pill warn">possible injection, treated as data</span>}
              <div className="snippet">{c.snippet}</div>
            </li>
          ))}
        </ol>
      )}
      <footer className="muted">
        ${final.cost.total_usd.toFixed(4)} · {final.cost.llm_calls} model calls · {final.cost.seconds}s
      </footer>
    </section>
  );
}
