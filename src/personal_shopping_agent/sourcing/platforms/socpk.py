"""Parser for the rendered Geekerwan SOCPK composite chip ranking page.

Only the table a browser renders for a normal visitor is read. The site's encoded data scripts are
never decoded, executed outside the browser, or requested directly.
"""

# ruff: noqa: RUF001 -- Chinese source copy intentionally uses full-width punctuation.

from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit

from personal_shopping_agent.sourcing.benchmarks import ChipBenchmarkEntry, ChipBenchmarkReference
from personal_shopping_agent.sourcing.discovery import CollectedPage

SOCPK_HOST = "www.socpk.com"
SOCPK_DOMAIN = "socpk.com"
SOCPK_OVERALL_URL = f"https://{SOCPK_HOST}/allperf/?brand=phone"
SOCPK_SOURCE_TITLE = "极客湾 SOCPK 手机/平板芯片综合性能排行"
SOCPK_DEFAULT_METHOD = "CPU权重70%，GPU权重30%，以骁龙865为基准（=100）"
MINIMUM_RANKED_CHIPS = 20


class BenchmarkPageParseError(ValueError):
    """Raised when a rendered ranking page is not a complete, recognizable table."""


class _RankingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str]] = []
        self.notice_parts: list[str] = []
        self._name_parts: list[str] | None = None
        self._score_parts: list[str] | None = None
        self._row_name = ""
        self._in_ratio = 0
        self._in_notice = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "tr":
            self._row_name = ""
            self._score_parts = None
        elif tag == "td" and "socName" in classes:
            self._name_parts = []
        elif tag == "div" and "ratio" in classes:
            self._in_ratio += 1
        elif tag == "a" and self._in_ratio:
            self._score_parts = []
        elif tag == "p" and attributes.get("id") == "notice":
            self._in_notice = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._name_parts is not None:
            self._row_name = " ".join("".join(self._name_parts).split())
            self._name_parts = None
        elif tag == "div" and self._in_ratio:
            self._in_ratio -= 1
        elif tag == "tr":
            score = "".join(self._score_parts or []).strip()
            if self._row_name and score:
                self.rows.append((self._row_name, score))
            self._row_name = ""
            self._score_parts = None
        elif tag == "p":
            self._in_notice = False

    def handle_data(self, data: str) -> None:
        if self._name_parts is not None:
            self._name_parts.append(data)
        elif self._score_parts is not None and self._in_ratio:
            self._score_parts.append(data)
        elif self._in_notice:
            self.notice_parts.append(data)


class SocpkRankingParser:
    """Translate one rendered SOCPK composite ranking snapshot into a dated reference."""

    def parse(self, page: CollectedPage) -> ChipBenchmarkReference:
        hostname = (urlsplit(str(page.final_url)).hostname or "").lower()
        if hostname not in {SOCPK_HOST, SOCPK_DOMAIN}:
            raise BenchmarkPageParseError("The ranking snapshot must come from socpk.com.")

        parser = _RankingParser()
        parser.feed(page.html)
        parser.close()

        entries: dict[str, ChipBenchmarkEntry] = {}
        for name, raw_score in parser.rows:
            try:
                score = Decimal(raw_score)
            except InvalidOperation as error:
                raise BenchmarkPageParseError("A ranking row has a non-numeric score.") from error
            if name in entries and entries[name].score != score:
                raise BenchmarkPageParseError("A chip appears twice with different scores.")
            entries[name] = ChipBenchmarkEntry(name=name, score=score)
        if len(entries) < MINIMUM_RANKED_CHIPS:
            raise BenchmarkPageParseError(
                "The ranking table did not render completely; nothing was saved."
            )

        method = " ".join("".join(parser.notice_parts).split()) or SOCPK_DEFAULT_METHOD
        return ChipBenchmarkReference(
            source_url=page.final_url,
            source_title=SOCPK_SOURCE_TITLE,
            method=method[:500],
            captured_at=page.captured_at,
            entries=tuple(entries.values()),
        )
