"""Parser for the rendered Geekerwan SOCPK composite chip ranking page.

Only the bar chart a browser renders for a normal visitor is read. The site's encoded data scripts
are never decoded, executed outside the browser, or requested directly.
"""

# ruff: noqa: RUF001 -- Chinese source copy intentionally uses full-width punctuation.

from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit

from personal_shopping_agent.sourcing.benchmarks import ChipBenchmarkEntry, ChipBenchmarkReference
from personal_shopping_agent.sourcing.discovery import CollectedPage

SOCPK_HOST = "www.socpk.com"
SOCPK_DOMAIN = "socpk.com"
SOCPK_OVERALL_URL = f"https://{SOCPK_HOST}/chart/chip-overall"
SOCPK_SOURCE_TITLE = "极客湾 SOCPK 手机芯片综合性能排行（历史项目）"
SOCPK_DEFAULT_METHOD = "CPU权重70%，GPU权重30%，以骁龙865为基准（=100）"
SOCPK_OVERALL_TITLE_MARK = "综合性能排行"
MINIMUM_RANKED_CHIPS = 20


class BenchmarkPageParseError(ValueError):
    """Raised when a rendered ranking page is not a complete, recognizable ranking."""


class _RankingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str]] = []
        self.title_parts: list[str] = []
        self.method_parts: list[str] = []
        self._name_parts: list[str] = []
        self._score_parts: list[str] = []
        self._target: list[str] | None = None
        self._row_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if tag == "div" and self._row_depth:
            self._row_depth += 1
        elif tag == "div" and "bar-row" in classes:
            self._row_depth = 1
            self._name_parts = []
            self._score_parts = []
        elif tag == "span" and self._row_depth and "bar-name" in classes:
            self._target = self._name_parts
        elif tag == "span" and self._row_depth and "bar-value" in classes:
            self._target = self._score_parts
        elif tag == "h1" and "page-title" in classes:
            self._target = self.title_parts
        elif tag == "p" and "page-subtitle" in classes:
            self._target = self.method_parts

    def handle_endtag(self, tag: str) -> None:
        if tag in {"span", "h1", "p"}:
            self._target = None
        elif tag == "div" and self._row_depth:
            self._row_depth -= 1
            if not self._row_depth:
                name = " ".join("".join(self._name_parts).split())
                score = "".join(self._score_parts).strip()
                if name and score:
                    self.rows.append((name, score))

    def handle_data(self, data: str) -> None:
        if self._target is not None:
            self._target.append(data)


class SocpkRankingParser:
    """Translate one rendered SOCPK composite ranking snapshot into a dated reference."""

    def parse(self, page: CollectedPage) -> ChipBenchmarkReference:
        hostname = (urlsplit(str(page.final_url)).hostname or "").lower()
        if hostname not in {SOCPK_HOST, SOCPK_DOMAIN}:
            raise BenchmarkPageParseError("The ranking snapshot must come from socpk.com.")

        parser = _RankingParser()
        parser.feed(page.html)
        parser.close()

        if SOCPK_OVERALL_TITLE_MARK not in "".join("".join(parser.title_parts).split()):
            raise BenchmarkPageParseError("The page is not the composite performance ranking.")

        entries: dict[str, ChipBenchmarkEntry] = {}
        for name, raw_score in parser.rows:
            try:
                entry = ChipBenchmarkEntry(name=name, score=Decimal(raw_score))
            except (InvalidOperation, ValueError) as error:
                raise BenchmarkPageParseError(
                    "A ranking row has an invalid chip name or score."
                ) from error
            if name in entries and entries[name].score != entry.score:
                raise BenchmarkPageParseError("A chip appears twice with different scores.")
            entries[name] = entry
        if len(entries) < MINIMUM_RANKED_CHIPS:
            raise BenchmarkPageParseError(
                "The ranking chart did not render completely; nothing was saved."
            )

        method = " ".join("".join(parser.method_parts).split()) or SOCPK_DEFAULT_METHOD
        return ChipBenchmarkReference(
            source_url=page.final_url,
            source_title=SOCPK_SOURCE_TITLE,
            method=method[:500],
            captured_at=page.captured_at,
            entries=tuple(entries.values()),
        )
