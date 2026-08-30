/* The application shell: rail, topbar, command palette.
 *
 * Replaces the row of tabs, which stopped scaling the moment the product grew past five
 * views. The rail groups by what a governance engineer is actually doing — reviewing
 * evidence, or configuring what gets tested — because those are different jobs done at
 * different times, and a flat list of ten items hides that.
 *
 * Counts in the rail come from real API state or do not appear. An empty rail on a fresh
 * deployment is the truthful thing to show.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { InvestigationRow, SystemInfo } from "../api";

export const VIEWS = [
  { id: "overview", label: "Overview", glyph: "◈", group: "govern" },
  { id: "evidence", label: "Evidence", glyph: "▤", group: "govern" },
  { id: "lineage", label: "Validity over time", glyph: "◷", group: "govern" },
  { id: "reaudits", label: "Re-audits", glyph: "↻", group: "govern" },
  { id: "configure", label: "Connections", glyph: "⚯", group: "operate" },
  { id: "infrastructure", label: "Infrastructure", glyph: "▦", group: "operate" },
  { id: "research", label: "Research", glyph: "⌥", group: "operate" },
] as const;

export type ViewId = (typeof VIEWS)[number]["id"];

const GROUPS: Array<{ id: string; label: string }> = [
  { id: "govern", label: "Govern" },
  { id: "operate", label: "Operate" },
];

const IDS = VIEWS.map((v) => v.id) as readonly string[];

export function useHashRoute(): [ViewId, (id: ViewId) => void] {
  const read = (): ViewId => {
    const raw = window.location.hash.replace(/^#\/?/, "").split("/")[0];
    return (IDS.includes(raw) ? raw : "overview") as ViewId;
  };
  const [view, setView] = useState<ViewId>(read);

  useEffect(() => {
    const onChange = () => setView(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return [
    view,
    (id: ViewId) => {
      window.location.hash = `/${id}`;
      setView(id);
    },
  ];
}

/* ------------------------------------------------------------------------- rail --- */

export function Rail({
  view,
  onNavigate,
  collapsed,
  counts,
  system,
}: {
  view: ViewId;
  onNavigate: (id: ViewId) => void;
  collapsed: boolean;
  counts: Partial<Record<ViewId, number>>;
  system: SystemInfo | null;
}) {
  return (
    <aside className="rail">
      <div className="rail__brand">
        <span className="rail__mark" aria-hidden="true" />
        <span className="rail__word">ARIADNE</span>
      </div>

      <nav className="rail__nav" aria-label="Sections">
        {GROUPS.map((group) => (
          <div key={group.id} className="rail__group">
            <p className="rail__group-label">{group.label}</p>
            {VIEWS.filter((entry) => entry.group === group.id).map((entry) => {
              const active = entry.id === view;
              const count = counts[entry.id];
              return (
                <button
                  key={entry.id}
                  type="button"
                  className={`rail__item${active ? " rail__item--active" : ""}`}
                  aria-current={active ? "page" : undefined}
                  title={collapsed ? entry.label : undefined}
                  onClick={() => onNavigate(entry.id)}
                >
                  <span className="rail__glyph" aria-hidden="true">
                    {entry.glyph}
                  </span>
                  <span className="rail__label">{entry.label}</span>
                  {count !== undefined && count > 0 && (
                    <span className="rail__count">{count}</span>
                  )}
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="rail__foot">
        <p className="rail__env">
          <span
            className={`rail__dot${system?.cloud.enabled ? " rail__dot--live" : ""}`}
            aria-hidden="true"
          />
          {system ? (system.cloud.enabled ? "LIVE" : "LOCAL") : "…"}
        </p>
        {system && (
          <p className="rail__foot-detail">
            {system.cloud.enabled ? (
              <>
                Google Cloud
                <br />
                {system.cloud.region}
              </>
            ) : (
              "in-process"
            )}
          </p>
        )}
      </div>
    </aside>
  );
}

/* ----------------------------------------------------------------------- topbar --- */

export function Topbar({
  view,
  collapsed,
  onCollapse,
  onOpenCommand,
  system,
}: {
  view: ViewId;
  collapsed: boolean;
  onCollapse: () => void;
  onOpenCommand: () => void;
  system: SystemInfo | null;
}) {
  const current = VIEWS.find((entry) => entry.id === view);
  return (
    <header className="topbar">
      <button
        type="button"
        className="topbar__collapse"
        onClick={onCollapse}
        aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
      >
        {collapsed ? "»" : "«"}
      </button>

      <nav className="topbar__crumbs" aria-label="Breadcrumb">
        <span>Ariadne</span>
        <span className="topbar__crumb-sep" aria-hidden="true">
          /
        </span>
        <span className="topbar__crumb--current">{current?.label ?? "Overview"}</span>
      </nav>

      <span className="topbar__spacer" />

      <button type="button" className="topbar__search" onClick={onOpenCommand}>
        <span aria-hidden="true">⌕</span>
        <span>Search claims, evidence, models</span>
        <kbd className="topbar__kbd">Ctrl K</kbd>
      </button>

      <span
        className={`topbar__env${system?.cloud.enabled ? " topbar__env--live" : ""}`}
        title={
          system?.reasoner.is_language_model
            ? `reasoner: ${system.reasoner.model}`
            : "reasoner: offline deterministic"
        }
      >
        {system ? (system.cloud.enabled ? "LIVE" : "LOCAL") : "…"}
      </span>
    </header>
  );
}

/* -------------------------------------------------------------- command palette --- */

interface Command {
  group: string;
  label: string;
  hint?: string;
  run: () => void;
}

export function CommandPalette({
  open,
  onClose,
  onNavigate,
  rows,
  onSelectInvestigation,
}: {
  open: boolean;
  onClose: () => void;
  onNavigate: (id: ViewId) => void;
  rows: InvestigationRow[];
  onSelectInvestigation: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      inputRef.current?.focus();
    }
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    const navigation: Command[] = VIEWS.map((entry) => ({
      group: "Go to",
      label: entry.label,
      hint: `#/${entry.id}`,
      run: () => onNavigate(entry.id),
    }));
    // Investigations are searchable by what a person would actually remember about them:
    // the model version and the verdict, not the content-addressed id.
    const investigations: Command[] = rows.map((row) => ({
      group: "Investigations",
      label: `v${row.model_version} · ${row.distribution_version}`,
      hint: row.verdict?.status ?? row.state,
      run: () => {
        onSelectInvestigation(row.id);
        onNavigate("evidence");
      },
    }));
    return [...navigation, ...investigations];
  }, [rows, onNavigate, onSelectInvestigation]);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands.slice(0, 12);
    return commands
      .filter(
        (command) =>
          command.label.toLowerCase().includes(needle) ||
          (command.hint ?? "").toLowerCase().includes(needle) ||
          command.group.toLowerCase().includes(needle),
      )
      .slice(0, 12);
  }, [commands, query]);

  const onKey = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.key === "Escape") return onClose();
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setCursor((c) => Math.min(c + 1, matches.length - 1));
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setCursor((c) => Math.max(c - 1, 0));
      }
      if (event.key === "Enter" && matches[cursor]) {
        matches[cursor].run();
        onClose();
      }
    },
    [matches, cursor, onClose],
  );

  if (!open) return null;

  let lastGroup = "";
  return (
    <div
      className="cmdk"
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      onClick={onClose}
    >
      <div className="cmdk__panel" onClick={(event) => event.stopPropagation()}>
        <input
          ref={inputRef}
          className="cmdk__input"
          placeholder="Search claims, evidence, models…"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setCursor(0);
          }}
          onKeyDown={onKey}
          aria-label="Search"
        />
        <div className="cmdk__list">
          {matches.length === 0 ? (
            <p className="cmdk__empty">Nothing matches “{query}”.</p>
          ) : (
            matches.map((command, index) => {
              const header = command.group !== lastGroup ? command.group : null;
              lastGroup = command.group;
              return (
                <div key={`${command.group}-${command.label}-${index}`}>
                  {header && <p className="cmdk__group">{header}</p>}
                  <button
                    type="button"
                    className={`cmdk__item${index === cursor ? " cmdk__item--active" : ""}`}
                    onMouseEnter={() => setCursor(index)}
                    onClick={() => {
                      command.run();
                      onClose();
                    }}
                  >
                    <span>{command.label}</span>
                    {command.hint && <span className="cmdk__item-hint">{command.hint}</span>}
                  </button>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

/** Ctrl/Cmd-K, and nothing else. One shortcut people already know. */
export function useCommandShortcut(onOpen: () => void) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        onOpen();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onOpen]);
}
