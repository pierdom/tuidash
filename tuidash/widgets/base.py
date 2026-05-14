from textual.widget import Widget
from textual.reactive import reactive


class DashWidget(Widget):
    """Base class for all dashboard widgets with title and loading state."""

    title: str = ""
    error: reactive[str | None] = reactive(None)

    DEFAULT_CSS = """
    DashWidget {
        border: solid $primary-darken-2;
        padding: 0 1;
    }
    DashWidget:focus {
        border: solid $accent;
    }
    """
