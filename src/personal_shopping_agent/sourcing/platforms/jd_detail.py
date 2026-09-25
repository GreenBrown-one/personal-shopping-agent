"""Fixture-driven JD detail parser with explicit price and provenance semantics."""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import urlsplit

from personal_shopping_agent.domain import StoreType
from personal_shopping_agent.sourcing import (
    CollectedPage,
    PlatformAccessRestrictedError,
    PlatformCandidate,
    PlatformDetailParseError,
    PlatformProductDetail,
    PlatformSpecificationObservation,
    PlatformVariantOption,
)
from personal_shopping_agent.sourcing.platforms.jd import (
    JD_ITEM_HOST,
    JD_PLATFORM,
    normalize_jd_item_url,
)

_PRICE_PATTERN = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")
_FULLWIDTH_COLON = "\uff1a"
_RESTRICTION_MARKERS = ("请输入验证码", "安全验证", "访问过于频繁", "captcha")
_OFF_SHELF_MARKERS = ("该商品已下柜", "商品已下架")


@dataclass(slots=True)
class _Capture:
    kind: str
    depth: int
    parts: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class _RawVariant:
    attribute: str
    value: str
    sku: str
    selected: bool


class _JDDetailHTMLParser(HTMLParser):
    _VOID_TAGS = frozenset({"area", "base", "br", "embed", "hr", "img", "input", "link", "meta"})
    _FIELD_CLASSES: ClassVar[dict[str, str]] = {
        "sku-name": "title",
        "shop-name": "seller",
        "store-type": "store_type",
        "stock-address": "region",
        "store-prompt": "stock",
        "prom-item": "promotion",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self.specifications: list[tuple[str, str]] = []
        self.variants: list[_RawVariant] = []
        self.main_sku: str | None = None
        self._depth = 0
        self._capture: _Capture | None = None
        self._specification_depth: int | None = None
        self._variant_group: tuple[int, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = frozenset((attributes.get("class") or "").split())
        if tag not in self._VOID_TAGS:
            self._depth += 1

        if "itemInfo-wrap" in classes and self.main_sku is None:
            self.main_sku = attributes.get("data-sku")
        if "parameter2" in classes:
            self._specification_depth = self._depth
        if "choose-attr" in classes:
            group_name = _clean_text([attributes.get("data-type") or ""])
            if group_name:
                self._variant_group = (self._depth, group_name)

        variant_group = self._variant_group
        if variant_group is not None and "item" in classes:
            self._record_variant(variant_group, attributes, classes)

        if self._capture is None:
            capture_kind = self._capture_kind(tag, classes, attributes)
            if capture_kind is not None:
                self._capture = _Capture(kind=capture_kind, depth=self._depth)

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        capture = self._capture
        if capture is not None and capture.depth == self._depth:
            self._finish_capture(capture)
            self._capture = None

        if self._variant_group is not None and self._variant_group[0] == self._depth:
            self._variant_group = None
        if self._specification_depth == self._depth:
            self._specification_depth = None
        if tag not in self._VOID_TAGS:
            self._depth -= 1

    def close(self) -> None:
        super().close()
        if self._capture is not None:
            self._finish_capture(self._capture)
            self._capture = None

    def _capture_kind(
        self,
        tag: str,
        classes: frozenset[str],
        attributes: dict[str, str | None],
    ) -> str | None:
        if tag == "li" and self._specification_depth is not None:
            return "specification"
        if "p-price" in classes:
            price_kind = attributes.get("data-price-kind") or "displayed"
            return price_kind if price_kind in {"list", "displayed", "conditional"} else None
        return next(
            (value for key, value in self._FIELD_CLASSES.items() if key in classes),
            None,
        )

    def _record_variant(
        self,
        group: tuple[int, str],
        attributes: dict[str, str | None],
        classes: frozenset[str],
    ) -> None:
        sku = _clean_text([attributes.get("data-sku") or ""])
        value = _clean_text([attributes.get("data-value") or ""])
        if sku and value:
            self.variants.append(
                _RawVariant(
                    attribute=group[1],
                    value=value,
                    sku=sku,
                    selected=bool(classes & {"selected", "hover"}),
                )
            )

    def _finish_capture(self, capture: _Capture) -> None:
        value = _clean_text(capture.parts)
        if not value:
            return
        if capture.kind == "specification":
            specification = _split_specification(value)
            if specification is not None:
                self.specifications.append(specification)
            return
        self.fields.setdefault(capture.kind, []).append(value)


def _clean_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _split_specification(value: str) -> tuple[str, str] | None:
    separator = _FULLWIDTH_COLON if _FULLWIDTH_COLON in value else ":" if ":" in value else None
    if separator is None:
        return None
    key, raw_value = (part.strip() for part in value.split(separator, 1))
    return (key, raw_value) if key and raw_value else None


def _parse_price(values: list[str]) -> Decimal | None:
    for value in values:
        match = _PRICE_PATTERN.search(value)
        if match is not None:
            return Decimal(match.group(0).replace(",", ""))
    return None


def classify_jd_store(seller: str | None, label: str | None) -> tuple[StoreType, str | None]:
    """Classify only explicit platform-controlled store labels or exact seller suffixes."""

    evidence = " ".join(part for part in (label, seller) if part)
    if "京东自营" in evidence:
        return StoreType.PLATFORM_SELF_OPERATED, evidence
    if label in {"品牌旗舰店", "官方旗舰店"} or (
        seller is not None and seller.endswith(("品牌旗舰店", "官方旗舰店"))
    ):
        return StoreType.BRAND_FLAGSHIP, evidence
    return StoreType.UNKNOWN, evidence or None


def normalize_jd_stock(status: str | None, *, off_shelf: bool) -> bool | None:
    """Map only explicit stock phrases; ambiguous delivery copy remains unknown."""

    if off_shelf or (status is not None and "无货" in status):
        return False
    if status is not None and any(marker in status for marker in ("有货", "现货")):
        return True
    return None


def _first(values: list[str]) -> str | None:
    return values[0] if values else None


def _specification_value(specifications: list[tuple[str, str]], key: str) -> str | None:
    return next((value for item_key, value in specifications if item_key == key), None)


class JDDetailAdapter:
    """Translate one inert JD item snapshot into an unverified detail observation."""

    @property
    def platform(self) -> str:
        return JD_PLATFORM

    def parse_detail(
        self,
        candidate: PlatformCandidate,
        page: CollectedPage,
    ) -> PlatformProductDetail:
        if candidate.platform != self.platform:
            raise PlatformDetailParseError(
                "platform_mismatch", "The candidate is not a JD listing."
            )
        self._validate_identity(candidate, page)

        lowered_html = page.html.lower()
        if any(marker.lower() in lowered_html for marker in _RESTRICTION_MARKERS):
            raise PlatformAccessRestrictedError(
                "jd_access_restricted",
                "JD requested human verification; automated collection stopped.",
            )

        parser = _JDDetailHTMLParser()
        parser.feed(page.html)
        parser.close()
        if parser.main_sku is not None and parser.main_sku != candidate.external_id:
            raise PlatformDetailParseError(
                "detail_sku_mismatch", "The JD detail DOM does not match the candidate SKU."
            )

        title = _first(parser.fields.get("title", []))
        if title is None:
            raise PlatformDetailParseError(
                "detail_title_missing", "The JD detail title could not be parsed."
            )

        seller = _first(parser.fields.get("seller", []))
        store_label = _first(parser.fields.get("store_type", []))
        store_type, store_basis = classify_jd_store(seller, store_label)
        stock_status = _first(parser.fields.get("stock", []))
        off_shelf = any(marker in page.html for marker in _OFF_SHELF_MARKERS)
        variants = tuple(
            PlatformVariantOption(
                attribute=variant.attribute,
                value=variant.value,
                sku=variant.sku,
                selected=variant.selected,
            )
            for variant in parser.variants
        )
        selected_values = [
            f"{variant.attribute}:{variant.value}" for variant in variants if variant.selected
        ]
        specifications = tuple(
            PlatformSpecificationObservation(key=key, raw_value=value)
            for key, value in parser.specifications
        )

        return PlatformProductDetail.model_validate(
            {
                "platform": self.platform,
                "external_id": candidate.external_id,
                "title": title,
                "product_url": str(page.final_url),
                "captured_at": page.captured_at,
                "brand": _specification_value(parser.specifications, "品牌"),
                "model": _specification_value(parser.specifications, "型号"),
                "seller_name": seller,
                "store_type": store_type,
                "store_type_basis": store_basis,
                "list_price": _parse_price(parser.fields.get("list", [])),
                "displayed_price": _parse_price(parser.fields.get("displayed", [])),
                "conditional_price": _parse_price(parser.fields.get("conditional", [])),
                "promotion_conditions": tuple(parser.fields.get("promotion", [])),
                "selected_variant": "; ".join(selected_values) or None,
                "variant_options": variants,
                "region": _first(parser.fields.get("region", [])),
                "stock_status": stock_status,
                "in_stock": normalize_jd_stock(stock_status, off_shelf=off_shelf),
                "is_off_shelf": off_shelf,
                "specifications": specifications,
            }
        )

    @staticmethod
    def _validate_identity(candidate: PlatformCandidate, page: CollectedPage) -> None:
        final_url = str(page.final_url)
        expected_url = normalize_jd_item_url(final_url, candidate.external_id)
        if expected_url is None or (urlsplit(final_url).hostname or "").lower() != JD_ITEM_HOST:
            raise PlatformDetailParseError(
                "detail_url_invalid", "The detail snapshot is not the expected JD item URL."
            )
        if str(candidate.product_url).rstrip("/") != expected_url:
            raise PlatformDetailParseError(
                "candidate_url_mismatch", "The candidate URL does not match the detail SKU."
            )
