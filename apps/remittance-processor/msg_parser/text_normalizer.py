from html.parser import HTMLParser


class _SafeTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed_tag: str | None = None

    def handle_starttag(self, tag: str, attrs):
        if tag in {"script", "style", "head"}:
            self.suppressed_tag = tag
        elif tag in {"br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag == self.suppressed_tag:
            self.suppressed_tag = None
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if self.suppressed_tag is None:
            self.parts.append(data)


def html_to_plain_text(html: str | None) -> str:
    if not html:
        return ""
    parser = _SafeTextParser()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)