"""The session prompt and the archive the agent reads.

Nothing here knows what a task is about. The sentences that do come from three places: the adapter's
PromptText, the task's declaration (which metrics exist, how they read, which checks gate a version, the
fields of the report), and the artifact kind (how one session's work is summarised and shown).
"""
from __future__ import annotations

from importlib import resources
import re

from ..artifacts import ArtifactKind, artifact_kind
from .adapter import PromptText
from .archive import TaskStore
from .schema import Declaration
from .selection import best_node, reference_node
from .types import Node

REPORT_BEGIN, REPORT_END = "SESSION-REPORT-BEGIN", "SESSION-REPORT-END"
REPORT = re.compile(re.escape(REPORT_BEGIN) + r"\s*(.*?)\s*" + re.escape(REPORT_END), re.S)


def template(name: str = "session.md") -> str:
    return resources.files("infinievolve").joinpath("prompts", name).read_text()


def render(text: str, **values) -> str:
    """Replace {name} placeholders only for the given names (task text may contain braces)."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


# -- the report every session ends with ---------------------------------------------------
def report_block(declaration: Declaration) -> str:
    lines = [f"{name}: <{hint}>" if hint else f"{name}:" for name, hint in declaration.report_fields]
    return "\n".join([REPORT_BEGIN, *lines, REPORT_END])


def extract_report(messages) -> str:
    """The last report block in what the agent said (one text, or its messages in order).

    Without a block, the end of the last message stands in, marked as such."""
    texts = [messages] if isinstance(messages, str) else [text for text in messages if text]
    for text in reversed(texts):
        blocks = REPORT.findall(text)
        if blocks:
            return blocks[-1].strip()
    if texts and texts[-1].strip():
        return "(no report block; the end of the last message follows)\n" + texts[-1].strip()[-3000:]
    return ""


# -- small formatters ---------------------------------------------------------------------
def minutes(seconds: float | None, digits: int = 0) -> str:
    return "-" if seconds is None else f"{seconds / 60:.{digits}f}"


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[... {len(text) - limit} more characters in the full archive]"


def rules_text(rules) -> str:
    """Campaign rules as their own section ("" when the campaign sets none)."""
    rules = list(rules or [])
    if not rules:
        return ""
    return "\n\n## Campaign rules\n\n" + "\n".join(f"- {rule}" for rule in rules)


def metric_value(node: Node, name: str):
    """A node's value of one metric: its ranking value (which a confirmation may have replaced), else what
    the archived version scored."""
    if name == node.rank_metric and node.rank_value is not None:
        return node.rank_value
    return (node.chosen().get("metrics") or {}).get(name)


def failures(node: Node, declaration: Declaration) -> str:
    """Why versions of this node failed: the evaluator's reasons, else the names of the failed checks.
    Nothing for a valid node: what an evaluator remarks about valid work is not a failure."""
    if node.valid:
        return ""
    reasons = sorted({str(reason) for row in node.scores for reason in (row.get("reasons") or [])})
    if reasons:
        return ", ".join(reasons)
    row = node.chosen() or (node.scores[-1] if node.scores else {})
    failed = declaration.failed(row.get("checks"))
    return ", ".join(failed)


# -- what the agent reads about earlier sessions ---------------------------------------------
NO_BASELINE = ("The unmodified baseline does not pass the task's checks, so it has no value to compare "
               "with; sessions are compared with each other")
WITHHELD = "score withheld"
HIDDEN = ("Scores of earlier sessions are withheld: this task is scored on something its sessions must not "
          "learn from, so a score is not feedback for later sessions. The framework still uses it to choose "
          "the starting point. Rely on what you can check yourself.")


def describe(node: Node, declaration: Declaration, hidden: bool = False) -> str:
    """A node's shown metrics in one phrase, the ranking metric first."""
    if hidden:
        return WITHHELD
    names = [declaration.rank_by] + [name for name in declaration.show if name != declaration.rank_by]
    return ", ".join(f"{name} {declaration.metrics[name].text(metric_value(node, name))}" for name in names)


def summary_table(nodes: list[Node], declaration: Declaration, hidden: bool = False) -> str:
    shown = [] if hidden else list(declaration.show)
    header = ["session", "parent", "start", "scored", "valid", *shown, "agent min", "failed"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for node in nodes:
        cells = [node.session_id, node.parent_id or "-", node.start_strategy, node.chosen_label or "-",
                 "yes" if node.valid else "no",
                 *[declaration.metrics[name].text(metric_value(node, name)) for name in shown],
                 minutes(node.timing.get("agent_seconds")), failures(node, declaration) or "-"]
        lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
    if hidden:
        lines += ["", HIDDEN]
    else:
        lines += ["", f"Sessions are ranked by `{declaration.rank_by}` "
                  f"({'higher' if declaration.rank.direction == 'max' else 'lower'} is better)."]
        root = next((node for node in nodes if node.is_root), None)
        if root is not None and not root.valid:
            reference = reference_node(nodes, declaration.rank)
            lines += ["", NO_BASELINE + (f", with **{reference.session_id}**, the first valid one, as the "
                                         "reference." if reference else ".")]
    return "\n".join(lines)


def work_line(node: Node, kind: ArtifactKind) -> str:
    return "Work: " + kind.describe(node.artifact.get("summary") or {})


def archive_summary(nodes: list[Node], declaration: Declaration, report_chars: int, hidden: bool = False) -> str:
    kind = artifact_kind(declaration.artifact)
    best = best_node(nodes, declaration.rank)
    parts = [summary_table(nodes, declaration, hidden), ""]
    if best and not hidden:
        parts.append(f"Best so far: **{best.session_id}** with {describe(best, declaration)}.\n")
    for node in nodes:
        if node.is_root:
            continue
        parts.append(f"### {node.session_id} (from {node.parent_id or 'baseline'}, "
                     f"{describe(node, declaration, hidden)}, {'valid' if node.valid else 'not valid'})")
        if node.error:
            parts.append(f"Session error: {node.error}")
        if node.excluded:
            parts.append(f"Not ranked and not a starting point: {node.flag_notes()}.")
            if hidden:   # its report and work may carry what it learned about the hidden scoring
                parts += ["(report and work withheld)", ""]
                continue
        elif node.flags and not node.flags_cleared:
            parts.append(f"Note: {node.flag_notes()}.")
        parts.append(work_line(node, kind))
        parts.append(clip(node.report, report_chars) or "(the agent left no report)")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def archive_markdown(store: TaskStore, nodes: list[Node], declaration: Declaration, hidden: bool = False,
                     with_work: bool = True) -> str:
    """The full archive: the table, then every node's complete report and, unless the campaign carries
    reports only, its archived work."""
    kind = artifact_kind(declaration.artifact)
    parts = ["# Campaign archive", "", summary_table(nodes, declaration, hidden), ""]
    for node in nodes:
        parts.append(f"## {node.session_id}")
        parts.append(f"parent: {node.parent_id or '-'}; start: {node.start_strategy}; scored version: "
                     f"{node.chosen_label or '-'}; {describe(node, declaration, hidden)}; valid: {node.valid}")
        if hidden and node.excluded:
            parts += [f"Not ranked and not a starting point: {node.flag_notes()}. Report and work withheld.", ""]
            continue
        if node.flags and not node.flags_cleared:
            parts.append(f"Flags: {node.flag_notes()}")
        for row in ([] if hidden else node.scores):
            values = " ".join(f"{name}={declaration.metrics[name].text(value)}"
                              for name, value in (row.get("metrics") or {}).items() if name in declaration.metrics)
            parts.append(f"- {row.get('label')}: valid={declaration.valid(row.get('checks'))} {values} "
                         f"failed={row.get('reasons') or []}")
        if node.report:
            parts += ["", "### Report", node.report.strip()]
        shown = kind.render(store.artifact(node.session_id)) if with_work and not node.is_root else None
        if shown:
            parts += ["", "### Work", shown]
        parts.append("")
    return "\n".join(parts)


ARCHIVE_INTRO = " What earlier sessions tried and what came of it is in the archive."
ARCHIVE_RULE = ("- Build on the archive, but read it critically. Check the claims your plan depends on "
                "yourself, and let your own analysis decide what you do. Do not repeat an approach that "
                "already failed unless you have a concrete reason it will work now, and say what is new in "
                "yours.\n")


def archived(nodes: list[Node]) -> bool:
    """Whether any earlier session is visible to this one; the whole archive wording hangs off it."""
    return any(not node.is_root for node in nodes)


def archive_section(nodes: list[Node], declaration: Declaration, report_chars: int, location: str,
                    hidden: bool = False, with_work: bool = True) -> str:
    if not archived(nodes):
        return ""
    contents = "every report and every session's archived work" if with_work else "every report in full"
    return ("\n\n## Archive of earlier sessions\n\n" +
            archive_summary(nodes, declaration, report_chars, hidden).rstrip() +
            f"\n\nThe complete archive, with {contents}, is at {location} (read-only).")


def start_text(parent: Node | None, text: PromptText, anonymous: bool = False) -> str:
    if parent is None or parent.is_root:
        return "You start from the unmodified baseline."
    if anonymous:   # nothing is carried over, so naming the session it came from would be the archive by other means
        return f"You start from earlier work on this task, already in place. {text.continuing}"
    return (f"You start from the archived work of **{parent.session_id}** (scored version "
            f"`{parent.chosen_label}`), already in place. {text.continuing}")


def ranking_text(declaration: Declaration) -> str:
    """How sessions are ranked, said from the declaration alone, for adapters that add nothing to it."""
    metric = declaration.rank
    text = f"by `{metric.name}`, {'higher' if metric.direction == 'max' else 'lower'} being better"
    if declaration.gate is None:
        text += ", among versions that pass every check"
    elif declaration.gate:
        text += ", among versions that pass " + ", ".join(f"`{name}`" for name in declaration.gate)
    text += "."
    if declaration.confirm:
        text += (f" A new best is scored {declaration.confirm.samples} more time(s) and ranked by the "
                 f"{declaration.confirm.aggregate}.")
    return text


def session_prompt(*, text: PromptText, instruction: str, declaration: Declaration, parent: Node | None,
                   nodes: list[Node], carry: str, report_chars: int, location: str, hidden: bool = False,
                   rules=()) -> str:
    """The prompt of one session. nodes are the sessions it may know about (void ones already left out)."""
    shows_archive = carry != "none" and archived(nodes)
    return render(
        template(),
        goal=text.goal, working=text.working, ranking=text.ranking or ranking_text(declaration),
        feedback=f"- {text.feedback}\n" if text.feedback else "",
        persistence=text.persistence,
        archive_intro=ARCHIVE_INTRO if shows_archive else "",
        archive_rule=ARCHIVE_RULE if shows_archive else "",
        report_block=report_block(declaration),
        start=start_text(parent, text, anonymous=carry == "none"),
        rules=rules_text(rules),
        archive=(archive_section(nodes, declaration, report_chars, location, hidden, with_work=carry == "full")
                 if shows_archive else ""),
        instruction=instruction)   # last: nothing inside the task statement is treated as a placeholder
