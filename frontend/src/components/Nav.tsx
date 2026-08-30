/* Navigation, and the information architecture behind it.
 *
 * The console had grown to twelve stacked sections in one scroll. Everything on it was real
 * and none of it was findable, which is its own kind of dishonesty: a governance tool whose
 * answer to "where do I connect my model?" is "keep scrolling" has not actually delivered
 * the capability it built.
 *
 * Five views, each corresponding to a real workflow rather than to a resource that sounded
 * impressive in a list. There is no Models tab because there is one model; no Settings tab
 * because there are no settings; no Re-audits tab because a re-audit *is* an investigation
 * and giving it its own page would imply a queue the backend does not separately model.
 *
 * Routing is the URL hash rather than a router library. A judge should be able to send
 * somebody a link to the evidence, which is the only requirement here, and it costs no
 * dependency to meet.
 */

import { useEffect, useState } from "react";

export const VIEWS = [
  {
    id: "overview",
    label: "Overview",
    blurb: "What needs a person, and the events that start work",
  },
  {
    id: "evidence",
    label: "Evidence",
    blurb: "The verdict, why it was reached, and every investigation",
  },
  {
    id: "lineage",
    label: "Validity over time",
    blurb: "One claim across model versions, and the debt it accrues",
  },
  {
    id: "configure",
    label: "Configure",
    blurb: "Connections, intervention semantics, explanation sources",
  },
  {
    id: "infrastructure",
    label: "Infrastructure",
    blurb: "What is actually wired, and the fleet's write authority",
  },
] as const;

export type ViewId = (typeof VIEWS)[number]["id"];

const IDS = VIEWS.map((view) => view.id) as readonly string[];

/** The current view, kept in the URL so a link to the evidence is a link to the evidence. */
export function useHashRoute(): [ViewId, (id: ViewId) => void] {
  const read = (): ViewId => {
    const raw = window.location.hash.replace(/^#\/?/, "");
    return (IDS.includes(raw) ? raw : "overview") as ViewId;
  };
  const [view, setView] = useState<ViewId>(read);

  useEffect(() => {
    const onChange = () => setView(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const go = (id: ViewId) => {
    window.location.hash = `/${id}`;
    setView(id);
  };
  return [view, go];
}

export function Nav({
  view,
  onNavigate,
  attention,
}: {
  view: ViewId;
  onNavigate: (id: ViewId) => void;
  attention: number;
}) {
  return (
    <nav className="nav" aria-label="Console sections">
      <ul className="nav__list">
        {VIEWS.map((entry) => {
          const active = entry.id === view;
          return (
            <li key={entry.id}>
              <button
                type="button"
                className={`nav__tab${active ? " nav__tab--active" : ""}`}
                aria-current={active ? "page" : undefined}
                onClick={() => onNavigate(entry.id)}
              >
                <span className="nav__label">
                  {entry.label}
                  {entry.id === "overview" && attention > 0 && (
                    <span className="nav__badge" aria-label={`${attention} needing attention`}>
                      {attention}
                    </span>
                  )}
                </span>
                <span className="nav__blurb">{entry.blurb}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
