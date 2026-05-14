from textual.app import ComposeResult
from textual.widgets import Static
from .base import DashWidget


class VpsWidget(DashWidget):
    title = "VPS Status"

    DEFAULT_CSS = """
    VpsWidget {
        height: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("VPS — coming soon", id="vps-content")
