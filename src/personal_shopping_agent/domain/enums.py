"""Stable domain enumerations shared by core models and adapters."""

from enum import StrEnum


class StoreType(StrEnum):
    """Seller relationship used for provenance and risk evaluation."""

    PLATFORM_SELF_OPERATED = "platform_self_operated"
    BRAND_FLAGSHIP = "brand_flagship"
    AUTHORIZED_RETAILER = "authorized_retailer"
    THIRD_PARTY = "third_party"
    UNKNOWN = "unknown"


class EvidenceSourceType(StrEnum):
    """Evidence source classes ordered later by the evidence engine."""

    MANUFACTURER_OFFICIAL = "manufacturer_official"
    PLATFORM_SELF_OPERATED = "platform_self_operated"
    BRAND_FLAGSHIP = "brand_flagship"
    AUTHORIZED_RETAILER = "authorized_retailer"
    THIRD_PARTY_SELLER = "third_party_seller"
    PLATFORM_LISTING = "platform_listing"
    INDEPENDENT_REVIEW = "independent_review"
    USER_COMMENT = "user_comment"
    SEARCH_SNIPPET = "search_snippet"


class EvidenceSubjectType(StrEnum):
    """Domain object kinds to which an evidence observation can refer."""

    PRODUCT = "product"
    OFFER = "offer"
    SPECIFICATION = "specification"


class PriceKind(StrEnum):
    """Explicit price layers; they must never be silently conflated."""

    LIST = "list_price"
    DISPLAYED = "displayed_price"
    UNCONDITIONAL = "unconditional_price"
    CONDITIONAL = "conditional_price"
    ESTIMATED_TOTAL = "estimated_total_cost"
