"""A small, dependency-free reader for the landing pages.

It records every element in document order with its attributes and the text inside it, which is
all the build step, the smoke test and the page tests need. It uses only the standard-library
HTML parser, so it runs the same way on a laptop, in CI and against a deployed page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)

# Attributes whose value is a URL the browser may fetch or follow.
URL_ATTRIBUTES = ("href", "src", "srcset", "action", "formaction", "data", "poster", "cite")


@dataclass
class Element:
    """One element: its tag, attributes, position and the text inside it."""

    tag: str
    attrs: dict[str, str]
    index: int
    _text: list[str] = field(default_factory=list, repr=False)

    @property
    def text(self) -> str:
        """The element's text with runs of whitespace collapsed."""
        return " ".join("".join(self._text).split())

    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)


@dataclass
class Page:
    """A parsed page."""

    elements: list[Element]

    def find_all(self, tag: str) -> list[Element]:
        return [element for element in self.elements if element.tag == tag]

    def find(self, tag: str) -> Element | None:
        found = self.find_all(tag)
        return found[0] if found else None

    def by_testid(self, testid: str) -> list[Element]:
        return [e for e in self.elements if e.attrs.get("data-testid") == testid]

    def by_id(self, element_id: str) -> Element | None:
        for element in self.elements:
            if element.attrs.get("id") == element_id:
                return element
        return None

    def url_references(self) -> list[tuple[Element, str, str]]:
        """Every (element, attribute, value) that names a URL."""
        return [
            (element, name, element.attrs[name])
            for element in self.elements
            for name in URL_ATTRIBUTES
            if name in element.attrs
        ]

    @property
    def body_text(self) -> str:
        body = self.find("body")
        return body.text if body else ""


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self._open: list[Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(
            tag=tag,
            attrs={name: value or "" for name, value in attrs},
            index=len(self.elements),
        )
        self.elements.append(element)
        if tag not in VOID_ELEMENTS:
            self._open.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_ELEMENTS and self._open and self._open[-1].tag == tag:
            self._open.pop()

    def handle_endtag(self, tag: str) -> None:
        # Close back to the matching open element; stray end tags are ignored.
        for position in range(len(self._open) - 1, -1, -1):
            if self._open[position].tag == tag:
                del self._open[position:]
                return

    def handle_data(self, data: str) -> None:
        for element in self._open:
            element._text.append(data)


def parse_page(html: str) -> Page:
    """Parse an HTML document into a `Page`."""
    collector = _Collector()
    collector.feed(html)
    collector.close()
    return Page(elements=collector.elements)
