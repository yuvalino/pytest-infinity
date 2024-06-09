import os
import select
import subprocess
import sys
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

from flask import Flask, request
from rich.text import Text
from rich.traceback import Traceback
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import (
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Static,
    TabbedContent,
    TabPane,
)

REST_PORT = 7778
TEST_UPDATE_ROUTE = "test-update"


class InfinityREST(threading.Thread):
    def __init__(self, fn, daemon=True):
        super().__init__(daemon=daemon)
        self.app = Flask(type(self).__name__)

        @self.app.route(f"/{TEST_UPDATE_ROUTE}", methods=["POST"])
        def _fn():
            fn(request.json)
            return ""

    def run(self):
        self.app.run(debug=False, port=REST_PORT)


class InfinityTest(threading.Thread):
    def __init__(self, args: List[str], testdir: str, fn, daemon=True):
        super().__init__(daemon=daemon)
        self.fn = fn
        self.output = ""
        self.po = subprocess.Popen(
            [
                "python3",
                "-m",
                "pytest",
                "--color",
                "yes",
                "-nauto",
                "--dist",
                "loadgroup",
                "--xstress",
                "-v",
                "--publish",
                f"http://localhost:{REST_PORT}/{TEST_UPDATE_ROUTE}",
                "--pubdir",
                testdir,
            ]
            + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def run(self):
        try:
            if self.po.stdout is None:
                raise ValueError("subprocess has no stdout handle")
            os.set_blocking(self.po.stdout.fileno(), False)
            while select.select([self.po.stdout], [], []) != []:
                curr_output = self.po.stdout.read(0x1000)
                if not curr_output:
                    break
                self.output += curr_output.decode()
                self.fn(self.output)
        finally:
            if self.po.poll() is None:
                self.po.kill()
                self.po.wait()


@dataclass
class TestValue:
    success: int = 0
    fail: int = 0
    skip: int = 0

    def format(self) -> Text:
        text = Text()
        text += Text(f"[{self.success}]", style="green" if self.success else "")
        text += Text(f"[{self.fail}]", style="red" if self.fail else "")
        if self.skip:
            text += Text(f"[{self.skip}]", style="yellow")
        return text


class InfinityUI(App):
    CSS_PATH = "infinity.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def __init__(self, testdir: str, on_mount: Callable):
        super().__init__()
        self._logstr = ""
        self._testdir = testdir
        self._fn_on_mount = on_mount
        self._lock = threading.Lock()
        self._test_values: dict[str, TestValue] = defaultdict(lambda: TestValue())

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="run-tab", id="tabs"):
            with TabPane("RUN", id="run-tab"):
                with VerticalScroll():
                    yield DataTable(id="run-table", show_cursor=False)
            with TabPane("OUTPUT", id="output-tab"):
                with VerticalScroll():
                    yield Static(id="output", expand=True)
            with TabPane("LOG"):
                with VerticalScroll():
                    yield Static(id="log", expand=True)
            with TabPane("FILES", id="files-tab"):
                yield DirectoryTree(self._testdir, id="files-tree")
                with VerticalScroll(id="files-view"):
                    yield Static(id="files-content", expand=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(TabbedContent).focus()
        self.print(f"test directory at {self._testdir}")
        self._fn_on_mount(self)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected):
        event.stop()
        files_content = self.query_one("#files-content", Static)
        files_view = self.query_one("#files-view", VerticalScroll)
        try:
            with open(event.path, "r") as r:
                file_data = r.read()
            text = Text.from_ansi(file_data)
            files_content.update(text)
            files_view.scroll_home(animate=False)
        except Exception:
            files_content.update(Traceback(theme="github-dark", width=None))

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ):
        files_tree = self.query_one("#files-tree", DirectoryTree)
        if event.node.is_expanded:
            files_tree.reload_node(event.node)

    def on_key(self, event: events.Key):
        active_tab = self.query_one("#tabs", TabbedContent).active
        if active_tab == "files-tab":
            self.on_key_files_tab(event)

    def on_key_files_tab(self, event: events.Key):
        files_tree = self.query_one("#files-tree", DirectoryTree)
        if event.key == "down":
            files_tree.action_cursor_down()
        elif event.key == "up":
            files_tree.action_cursor_up()
        elif event.key == "space" or event.key == "enter":
            files_tree.action_select_cursor()
        else:
            return
        self.print(f"files-tab key event: {event.key}")

    async def add_test_report_entry(
        self, name: str, scope: str, result: str, path: str | None
    ):
        self.print(f"add-row {name} X {scope} X {result}")

        # Update table

        run_table = self.query_one("#run-table", DataTable)
        if name not in run_table.columns:
            run_table.add_column(name, key=name)
        if scope not in run_table.rows:
            run_table.add_row(key=scope, label=scope)

        value = self._test_values[f"{scope}@{name}"]
        if result == "fail":
            value.fail += 1
        elif result == "skip":
            value.skip += 1
        else:
            value.success += 1

        run_table.update_cell(scope, name, value.format(), update_width=True)

        # Update files
        if path is None:
            return
        files_tree = self.query_one("#files-tree", DirectoryTree)
        parts = Path(path).relative_to(self._testdir).parts
        parent = files_tree.root
        while parts and parent.is_expanded:
            next_node = next(
                (x for x in parent.children if x.data.path.name == parts[0]), None
            )
            if not next_node:
                files_tree.reload_node(parent)
                break
            parts = parts[1:]
            parent = next_node

    @work(thread=True)
    def print(self, line: str):
        with self._lock:
            self._logstr += line + "\n"
            text = Text.from_ansi(self._logstr)
        log_view = self.query_one("#log", Static)
        self.call_from_thread(log_view.update, text)

    def add_test_report(self, data: dict):
        self.call_from_thread(
            self.add_test_report_entry,
            data["name"],
            data["xdist_scope"],
            data["result"],
            data["pubdir_path"],
        )

    def update_output(self, output: str):
        output_view = self.query_one("#output", Static)
        text = Text.from_ansi(
            output,
        )
        self.call_from_thread(output_view.update, text)


def main_threads(ui: InfinityUI):
    InfinityREST(fn=lambda data: ui.add_test_report(data)).start()

    InfinityTest(
        args=sys.argv[1:],
        testdir=ui._testdir,
        fn=lambda output: ui.update_output(output),
    ).start()


def main():
    testdir = f"/tmp/pytest-infinity/{str(uuid.uuid4())}"
    os.makedirs(testdir)

    InfinityUI(testdir=testdir, on_mount=main_threads).run()


if __name__ == "__main__":
    main()
