"""Fixture-driven JD search adapter with no live access or private API use."""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import urlencode, urlsplit

from personal_shopping_agent.sourcing.discovery import (
    CollectedPage,
    PlatformAccessRestrictedError,
    PlatformCandidate,
    PlatformSearchResult,
)

JD_PLATFORM = "jd"
JD_SEARCH_HOST = "search.jd.com"
JD_ITEM_HOST = "item.jd.com"
JD_SIGN_IN_HOST = "passport.jd.com"
JD_SIGN_IN_URL = f"https://{JD_SIGN_IN_HOST}/new/login.aspx"
JD_HOME_HOST = "www.jd.com"
# Registrable domains JD pages load their own scripts, styles, images, and price data from.
JD_SUBRESOURCE_DOMAINS = ("jd.com", "360buyimg.com", "3.cn")
_SKU_PATTERN = re.compile(r"\d{1,32}")
_PRICE_PATTERN = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")
_RESTRICTION_MARKERS = (
    "请输入验证码",
    "安全验证",
    "访问过于频繁",
    "captcha",
)


@dataclass(slots=True)
class _CandidateBuffer:
    sku: str
    link: str | None = None
    title_parts: list[str] = field(default_factory=list[str])
    price_parts: list[str] = field(default_factory=list[str])
    seller_parts: list[str] = field(default_factory=list[str])
    review_parts: list[str] = field(default_factory=list[str])
    badges: list[str] = field(default_factory=list[str])


class _JDSearchHTMLParser(HTMLParser):
    _VOID_TAGS = frozenset({"area", "base", "br", "embed", "hr", "img", "input", "link", "meta"})
    _FIELD_CLASSES: ClassVar[dict[str, str]] = {
        "p-name": "title",
        "p-price": "price",
        "p-shop": "seller",
        "p-commit": "review",
        "goods-icons": "badge",
        "goods-icons4": "badge",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[_CandidateBuffer] = []
        self._current: _CandidateBuffer | None = None
        self._depth: int = 0
        self._capture_field: str | None = None
        self._capture_depth: int = 0
        self._capture_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = frozenset((attributes.get("class") or "").split())
        if self._current is None:
            if tag == "li" and "gl-item" in classes:
                self._current = _CandidateBuffer(sku=attributes.get("data-sku") or "")
                self._depth = 1
            return

        if tag not in self._VOID_TAGS:
            self._depth += 1

        if tag == "a" and self._current.link is None:
            self._current.link = attributes.get("href")

        if self._capture_field is None:
            field_name = next(
                (value for key, value in self._FIELD_CLASSES.items() if key in classes),
                None,
            )
            if field_name is not None:
                self._capture_field = field_name
                self._capture_depth = self._depth
                self._capture_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_field is not None:
            self._capture_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        current = self._current
        if current is None:
            return

        if self._capture_field is not None and self._depth == self._capture_depth:
            value = _clean_text(self._capture_parts)
            if value:
                self._store_capture(current, self._capture_field, value)
            self._capture_field = None
            self._capture_parts = []

        if tag == "li" and self._depth == 1:
            self.candidates.append(current)
            self._current = None
            self._depth = 0
            return

        if tag not in self._VOID_TAGS:
            self._depth -= 1

    def close(self) -> None:
        super().close()
        if self._current is not None:
            self.candidates.append(self._current)
            self._current = None

    @staticmethod
    def _store_capture(current: _CandidateBuffer, field_name: str, value: str) -> None:
        if field_name == "badge":
            current.badges.append(value)
        else:
            parts = getattr(current, f"{field_name}_parts")
            parts.append(value)


def _clean_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _parse_price(parts: list[str]) -> Decimal | None:
    match = _PRICE_PATTERN.search(_clean_text(parts))
    if match is None:
        return None
    return Decimal(match.group(0).replace(",", ""))


def normalize_jd_item_url(link: str | None, sku: str) -> str | None:
    if link is None:
        return None
    if link.startswith("//"):
        link = f"https:{link}"
    parts = urlsplit(link)
    hostname = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port or 443
    except ValueError:
        return None
    if (
        parts.scheme.lower() != "https"
        or hostname != JD_ITEM_HOST
        or port != 443
        or parts.username is not None
        or parts.password is not None
        or parts.path != f"/{sku}.html"
    ):
        return None
    return f"https://{JD_ITEM_HOST}/{sku}.html"


class JDSearchAdapter:
    """Translate one inert JD search snapshot into unverified platform candidates."""

    @property
    def platform(self) -> str:
        return JD_PLATFORM

    def build_search_url(self, query: str) -> str:
        normalized_query = " ".join(query.split())
        if not normalized_query or len(normalized_query) > 200:
            raise ValueError("query must contain between 1 and 200 characters")
        parameters = urlencode({"keyword": normalized_query, "enc": "utf-8"})
        return f"https://{JD_SEARCH_HOST}/Search?{parameters}"

    def parse_search(
        self,
        query: str,
        page: CollectedPage,
        *,
        maximum_candidates: int,
    ) -> PlatformSearchResult:
        if not 1 <= maximum_candidates <= 100:
            raise ValueError("maximum_candidates must be between 1 and 100")
        if (urlsplit(str(page.final_url)).hostname or "").lower() != JD_SEARCH_HOST:
            raise ValueError("JD search snapshots must originate from search.jd.com")

        lowered_html = page.html.lower()
        if any(marker.lower() in lowered_html for marker in _RESTRICTION_MARKERS):
            raise PlatformAccessRestrictedError(
                "jd_access_restricted",
                "JD requested human verification; automated collection stopped.",
            )

        parser = _JDSearchHTMLParser()
        parser.feed(page.html)
        parser.close()

        candidates: list[PlatformCandidate] = []
        seen_skus: set[str] = set()
        for item in parser.candidates:
            if len(candidates) >= maximum_candidates:
                break
            candidate = self._to_candidate(item, page)
            if candidate is None or candidate.external_id in seen_skus:
                continue
            seen_skus.add(candidate.external_id)
            candidates.append(candidate)

        return PlatformSearchResult.model_validate(
            {
                "platform": self.platform,
                "query": " ".join(query.split()),
                "source_url": str(page.final_url),
                "captured_at": page.captured_at,
                "candidates": tuple(candidates),
            }
        )

    def _to_candidate(
        self, item: _CandidateBuffer, page: CollectedPage
    ) -> PlatformCandidate | None:
        sku = item.sku.strip()
        title = _clean_text(item.title_parts)
        product_url = normalize_jd_item_url(item.link, sku)
        if _SKU_PATTERN.fullmatch(sku) is None or not title or product_url is None:
            return None

        seller = _clean_text(item.seller_parts) or None
        review = _clean_text(item.review_parts) or None
        badges = tuple(dict.fromkeys(item.badges))
        return PlatformCandidate.model_validate(
            {
                "platform": self.platform,
                "external_id": sku,
                "title": title,
                "product_url": product_url,
                "displayed_price": _parse_price(item.price_parts),
                "seller_name": seller,
                "badges": badges,
                "review_summary": review,
            }
        )
