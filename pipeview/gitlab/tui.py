"""Curses project browser.

Thin over the headless core: every action here (search, track, report)
has a flag-driven twin in `pipeview gitlab …` subcommands. Pure list/window
logic lives in module-level functions so tests never need a terminal;
`curses` is imported lazily so import of this module never fails (native
Windows Python lacks it — the error message points at `windows-curses`).
"""

from __future__ import annotations

import os
import sys
import webbrowser

from pipeview.gitlab.api import GitLabError
from pipeview.gitlab.config import GitLabConfig

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a terminal)
# ---------------------------------------------------------------------------


def visible_window(cursor: int, count: int, height: int) -> tuple[int, int]:
    """First/last-plus-one indices of the visible slice keeping the cursor
    in view."""
    if count <= height or height <= 0:
        return 0, count
    start = min(max(0, cursor - height // 2), count - height)
    return start, start + height


def order_projects(projects: list[dict], tracked: list[str]) -> list[dict]:
    """Tracked projects first (stable within each group). Entries may be
    "group/app" or "group/app@ref" — either counts as tracked."""
    tracked_set = {GitLabConfig.parse_entry(e)[0] for e in tracked}
    is_tracked = [p for p in projects
                  if p.get("path_with_namespace") in tracked_set]
    rest = [p for p in projects
            if p.get("path_with_namespace") not in tracked_set]
    return is_tracked + rest


def order_refs(default_branch: str | None, branches: list[str],
               tags: list[str]) -> list[tuple[str, str]]:
    """(kind, name) list: default branch, other branches, then tags."""
    out: list[tuple[str, str]] = []
    if default_branch:
        out.append(("default", default_branch))
    out.extend(("branch", b) for b in branches if b != default_branch)
    out.extend(("tag", t) for t in tags)
    return out


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def open_report(path: str) -> bool:
    try:
        return webbrowser.open("file://" + os.path.abspath(path))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------

_HELP = [
    "  ↑/k ↓/j     move        PgUp/PgDn  page",
    "  enter       open project / generate report",
    "  /           search projects (server-side)",
    "  t           track/untrack (list: project; ref view: pin that ref)",
    "  o           open last generated report in browser",
    "  n           load next page of results",
    "  b/esc       back        r  refresh",
    "  q           quit        ?  this help",
]


def run_tui(client, config: GitLabConfig, host: str, *,
            outdir: str = "./pipeview-out", formats=("html", "json"),
            strategy: str = "auto", generate=None) -> int:
    """Entry point. `generate` is injectable for tests; defaults to
    pipeview.gitlab.report.generate_report."""
    try:
        import curses  # noqa: F401
    except ImportError:
        print(
            "The interactive browser needs the curses module, which this "
            "Python lacks.\nOn Windows: pip install windows-curses — or use "
            "the headless commands:\n  pipeview gitlab projects / report / "
            "track / sync (see pipeview gitlab --help)",
            file=sys.stderr,
        )
        return 2

    if generate is None:
        from pipeview.gitlab.report import generate_report as generate

    app = _App(client, config, host, outdir=outdir, formats=formats,
               strategy=strategy, generate=generate)
    import curses as _curses
    return _curses.wrapper(app.run)


class _App:
    def __init__(self, client, config, host, *, outdir, formats, strategy,
                 generate):
        self.client = client
        self.config = config
        self.host = host
        self.outdir = outdir
        self.formats = formats
        self.strategy = strategy
        self.generate = generate

        self.projects: list[dict] = []
        self.next_page: int | None = 1
        self.search = ""
        self.cursor = 0
        self.status = "loading projects…"
        self.last_report: str | None = None
        self.help_visible = False

        # project view state
        self.current: dict | None = None
        self.refs: list[tuple[str, str]] = []
        self.ref_cursor = 0

    # -- data ---------------------------------------------------------------

    def _load_projects(self, reset: bool) -> None:
        if reset:
            self.projects, self.next_page, self.cursor = [], 1, 0
        if self.next_page is None:
            return
        try:
            items, nxt = self.client.list_projects(
                search=self.search or None, page=self.next_page)
        except GitLabError as e:
            self.status = str(e)
            return
        self.projects.extend(items)
        self.next_page = nxt
        shown = len(self.projects)
        more = " (n: more)" if nxt else ""
        what = f" matching {self.search!r}" if self.search else ""
        self.status = f"{shown} project(s){what}{more}"

    def _open_project(self, proj: dict) -> None:
        path = proj.get("path_with_namespace")
        self.status = f"loading {path}…"
        try:
            full = self.client.get_project(path)
            branches, _ = self.client.list_branches(path)
            tags, _ = self.client.list_tags(path)
        except GitLabError as e:
            self.status = str(e)
            return
        self.current = full
        self.refs = order_refs(
            full.get("default_branch"),
            [b.get("name", "") for b in branches],
            [t.get("name", "") for t in tags],
        )
        self.ref_cursor = 0
        self.status = f"{path} — pick a ref, enter generates the report"

    def _generate(self) -> None:
        if not self.current or not self.refs:
            return
        path = self.current.get("path_with_namespace")
        _, ref = self.refs[self.ref_cursor]
        self.status = f"fetching {path}@{ref} ({self.strategy})…"
        self._paint()
        try:
            report, written = self.generate(
                self.client, path, ref=ref, outdir=self.outdir,
                formats=self.formats, strategy=self.strategy)
        except GitLabError as e:
            self.status = str(e)
            return
        html = next((p for p in written if p.endswith(".html")), None)
        self.last_report = html or (written[0] if written else None)
        sev = report.max_severity()
        verdict = f", diagnostics: {sev}" if sev else ""
        name = os.path.basename(self.last_report) if self.last_report else "?"
        self.status = f"generated {name}{verdict} — o opens it"

    def _toggle_track_project(self, project_path: str | None) -> None:
        """List view: bare-entry toggle. Untracking removes every ref."""
        if not project_path:
            return
        if self.config.is_tracked_any(self.host, project_path):
            removed = self.config.untrack_all(self.host, project_path)
            refs = " (all refs)" if removed > 1 else ""
            self.status = f"untracked {project_path}{refs}"
        else:
            self.config.track(self.host, project_path)
            self.status = f"tracking {project_path} (default branch)"
        self.config.save()

    def _toggle_track_ref(self) -> None:
        """Project view: toggle the exact entry for the selected ref.
        The default branch tracks as a bare entry (follows the default);
        any other ref pins project@ref."""
        if not self.current or not self.refs:
            return
        project_path = self.current.get("path_with_namespace")
        kind, name = self.refs[self.ref_cursor]
        ref = None if kind == "default" else name
        entry = GitLabConfig.make_entry(project_path, ref)
        if self.config.is_tracked(self.host, project_path, ref):
            self.config.untrack(self.host, project_path, ref)
            self.status = f"untracked {entry}"
        else:
            self.config.track(self.host, project_path, ref)
            self.status = f"tracking {entry}"
        self.config.save()

    # -- curses loop --------------------------------------------------------

    def run(self, stdscr) -> int:
        import curses
        curses.curs_set(0)
        stdscr.keypad(True)
        self.scr = stdscr
        self._load_projects(reset=True)
        while True:
            self._paint()
            key = stdscr.getch()
            if self.help_visible:
                self.help_visible = False
                continue
            if self.current is None:
                if not self._handle_projects_key(key):
                    return 0
            else:
                self._handle_project_key(key)

    def _handle_projects_key(self, key) -> bool:
        import curses
        ordered = order_projects(self.projects, self.config.tracked(self.host))
        count = len(ordered)
        if key in (ord("q"),):
            return False
        elif key in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(max(0, count - 1), self.cursor + 1)
        elif key == curses.KEY_PPAGE:
            self.cursor = max(0, self.cursor - self._list_height())
        elif key == curses.KEY_NPAGE:
            self.cursor = min(max(0, count - 1), self.cursor + self._list_height())
        elif key == ord("g"):
            self.cursor = 0
        elif key == ord("G"):
            self.cursor = max(0, count - 1)
        elif key == ord("n"):
            self._load_projects(reset=False)
        elif key == ord("r"):
            self._load_projects(reset=True)
        elif key == ord("/"):
            self._prompt_search()
        elif key == ord("t") and count:
            self._toggle_track_project(ordered[self.cursor].get("path_with_namespace"))
        elif key == ord("o") and self.last_report:
            open_report(self.last_report)
        elif key == ord("?"):
            self.help_visible = True
        elif key in (curses.KEY_ENTER, 10, 13) and count:
            self._open_project(ordered[self.cursor])
        return True

    def _handle_project_key(self, key) -> None:
        import curses
        count = len(self.refs)
        if key in (ord("b"), ord("q"), 27):
            self.current = None
            self.status = ""
        elif key in (curses.KEY_UP, ord("k")):
            self.ref_cursor = max(0, self.ref_cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.ref_cursor = min(max(0, count - 1), self.ref_cursor + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            self._generate()
        elif key == ord("o") and self.last_report:
            open_report(self.last_report)
        elif key == ord("t") and self.current:
            self._toggle_track_ref()
        elif key == ord("?"):
            self.help_visible = True

    def _prompt_search(self) -> None:
        import curses
        h, w = self.scr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        try:
            self.scr.move(h - 1, 0)
            self.scr.clrtoeol()
            self.scr.addstr(h - 1, 0, "search: ")
            raw = self.scr.getstr(h - 1, len("search: "), 200)
            self.search = raw.decode("utf-8", errors="replace").strip()
        except curses.error:
            pass
        finally:
            curses.noecho()
            curses.curs_set(0)
        self._load_projects(reset=True)

    # -- painting -----------------------------------------------------------

    def _list_height(self) -> int:
        h, _ = self.scr.getmaxyx()
        return max(1, h - 4)   # header + column line + footer + status

    def _addstr(self, y, x, text, attr=0):
        import curses
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h:
            return
        try:
            self.scr.addstr(y, x, truncate(text, w - x - 1), attr)
        except curses.error:
            pass

    def _paint(self) -> None:
        import curses
        self.scr.erase()
        h, w = self.scr.getmaxyx()

        title = f"pipeview gitlab — {self.host}"
        if self.search and self.current is None:
            title += f" — search: {self.search!r}"
        self._addstr(0, 0, truncate(title, w - 1), curses.A_BOLD)

        if self.help_visible:
            for i, line in enumerate(_HELP):
                self._addstr(2 + i, 2, line)
            self._addstr(h - 1, 0, "any key to close help")
            self.scr.refresh()
            return

        if self.current is None:
            self._paint_projects()
        else:
            self._paint_project()

        self._addstr(
            h - 2, 0,
            "enter open · / search · t track · n more · o report · ? help · q quit",
            curses.A_DIM if hasattr(curses, "A_DIM") else 0,
        )
        self._addstr(h - 1, 0, self.status)
        self.scr.refresh()

    def _paint_projects(self) -> None:
        import curses
        ordered = order_projects(self.projects, self.config.tracked(self.host))
        height = self._list_height()
        start, end = visible_window(self.cursor, len(ordered), height)
        self._addstr(1, 0, f"{'':2}{'project':40}  last activity", curses.A_UNDERLINE)
        for row, idx in enumerate(range(start, min(end, len(ordered)))):
            p = ordered[idx]
            path = p.get("path_with_namespace") or "?"
            mark = "●" if self.config.is_tracked_any(self.host, path) else " "
            activity = (p.get("last_activity_at") or "")[:10]
            line = f"{mark} {path:40}  {activity}"
            attr = curses.A_REVERSE if idx == self.cursor else 0
            self._addstr(2 + row, 0, line, attr)
        if not ordered:
            self._addstr(3, 2, "no projects — / to search, r to refresh")

    def _paint_project(self) -> None:
        import curses
        p = self.current or {}
        path = p.get("path_with_namespace") or "?"
        self._addstr(1, 0, f"{path} — pick a ref:", curses.A_UNDERLINE)
        height = self._list_height()
        start, end = visible_window(self.ref_cursor, len(self.refs), height)
        for row, idx in enumerate(range(start, min(end, len(self.refs)))):
            kind, name = self.refs[idx]
            label = {"default": "default branch", "branch": "branch",
                     "tag": "tag"}[kind]
            ref = None if kind == "default" else name
            mark = "●" if self.config.is_tracked(self.host, path, ref) else " "
            attr = curses.A_REVERSE if idx == self.ref_cursor else 0
            self._addstr(2 + row, 0, f"{mark} {name:40}  {label}", attr)
        if not self.refs:
            self._addstr(3, 2, "no refs found (empty repository?)")
