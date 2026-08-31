"""Documentation must point at things that exist.

`test_readme_is_current.py` guards the README's claims. This guards the other thirteen
documents, which are read less often and therefore drift more freely.

The failure this prevents is small and corrosive: a document confidently naming
`backend/experiment_engine/verification.py` when the repository has no such file. Nobody
notices, because nothing executes a docs reference — the same reason the config options in
`docs/architecture-review.md` §F1–F8 went unread for a whole build, and the same reason
`run_demo.py`'s output went unasserted while it printed a lie about the hash chain (§F10).

Two things are checked here, and both are things a machine can settle:

  - **Every repository path a doc names in backticks actually resolves.** Design-pack
    references are deliberately written without a repo-shaped prefix (see the convention in
    `docs/decisions.md`), so they do not trip this.
  - **Every mermaid block declares a diagram type.** A malformed block does not degrade
    gracefully on GitHub — it renders an error box where the diagram should be, which is
    worse than having no diagram.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCS = sorted(REPO.glob("docs/*.md")) + [REPO / "README.md"]

# A backticked token that looks like a path into this repository.
REPO_PATH = re.compile(
    r"`((?:docs|tests|backend|benchmark|frontend|infra)/[A-Za-z0-9_./-]+?"
    r"\.(?:py|md|tf|yaml|yml|ts|tsx|json|conf|template))`"
)
MERMAID = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)

# Mermaid's diagram types, as used in this repository.
DIAGRAM_TYPES = (
    "flowchart", "graph", "sequenceDiagram", "stateDiagram", "stateDiagram-v2",
    "erDiagram", "classDiagram", "gantt", "pie", "journey",
)


def documents() -> list[pathlib.Path]:
    assert DOCS, "no documentation found to check"
    return DOCS


@pytest.mark.parametrize("doc", documents(), ids=lambda p: p.name)
class TestReferencedPathsResolve:
    def test_every_repository_path_it_names_exists(self, doc: pathlib.Path) -> None:
        """A doc naming a file that is not there sends a reader looking for nothing."""
        named = {m.split("::")[0] for m in REPO_PATH.findall(doc.read_text(encoding="utf-8"))}
        missing = sorted(p for p in named if not (REPO / p).exists())
        assert not missing, f"{doc.name} names paths that do not exist: {missing}"

    def test_every_markdown_link_resolves(self, doc: pathlib.Path) -> None:
        links = set(
            re.findall(r"\]\((docs/[^)#]+|[A-Z_]+\.md)\)", doc.read_text(encoding="utf-8"))
        )
        missing = sorted(link for link in links if not (REPO / link).exists())
        assert not missing, f"{doc.name} links to files that do not exist: {missing}"


@pytest.mark.parametrize("doc", documents(), ids=lambda p: p.name)
class TestMermaidBlocksAreWellFormed:
    """A broken diagram renders as an error box on GitHub, in place of the explanation."""

    def test_each_block_declares_a_diagram_type(self, doc: pathlib.Path) -> None:
        for index, block in enumerate(MERMAID.findall(doc.read_text(encoding="utf-8"))):
            lines = [line for line in block.splitlines() if line.strip()]
            assert lines, f"{doc.name} mermaid block {index} is empty"
            assert lines[0].strip().startswith(DIAGRAM_TYPES), (
                f"{doc.name} mermaid block {index} starts with {lines[0].strip()!r}, "
                f"which is not one of {DIAGRAM_TYPES}"
            )

    def test_brackets_and_quotes_balance(self, doc: pathlib.Path) -> None:
        """Catches the commonest hand-editing slip: a node label left half-closed.

        Restricted to node-and-edge diagrams. `erDiagram` spends braces on crow's-foot
        cardinality (`||--o{`) where they are not brackets at all, and `sequenceDiagram`
        uses its own participant syntax — counting pairs there reports a balanced diagram
        as broken, which is the exact failure this file exists to prevent.
        """
        for index, block in enumerate(MERMAID.findall(doc.read_text(encoding="utf-8"))):
            if not block.lstrip().startswith(("flowchart", "graph", "stateDiagram")):
                continue
            assert block.count('"') % 2 == 0, (
                f"{doc.name} mermaid block {index} has an odd number of quotes"
            )
            for opener, closer in (("[", "]"), ("(", ")"), ("{", "}")):
                assert block.count(opener) == block.count(closer), (
                    f"{doc.name} mermaid block {index}: "
                    f"{opener}{closer} are unbalanced ({block.count(opener)} vs "
                    f"{block.count(closer)})"
                )


class TestTheDocumentationSetIsCoherent:
    def test_the_readme_links_every_document(self) -> None:
        """Duplicated from the README's own suite on purpose.

        A document nobody can reach is a document nobody reads, and this is the check most
        likely to be forgotten when a new doc is added.
        """
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        unlinked = sorted(
            f"docs/{p.name}"
            for p in (REPO / "docs").glob("*.md")
            if f"docs/{p.name}" not in readme
        )
        assert not unlinked, f"docs exist but the README never links them: {unlinked}"

    def test_no_document_is_empty(self) -> None:
        empty = [d.name for d in documents() if len(d.read_text(encoding="utf-8").strip()) < 200]
        assert not empty, f"suspiciously short documents: {empty}"
