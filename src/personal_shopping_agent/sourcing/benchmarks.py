"""Independent chip benchmark reference and its conversion into product evidence.

Chip performance is never inferred from platform text. A chip named in a platform specification
is matched exactly (after fixed folding rules) against a locally stored, dated benchmark ranking;
only a unique match becomes one ``independent_review`` evidence row.
"""

# ruff: noqa: RUF001 -- Chinese source copy intentionally uses full-width punctuation.

import re
import unicodedata
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

from personal_shopping_agent.domain import (
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Product,
)

CHIP_PERFORMANCE_FIELD = "specifications.chip_performance"
CHIP_PERFORMANCE_UNIT = "SOCPK"
CHIP_SPECIFICATION_KEYS = frozenset(
    {"cpu型号", "cpu", "处理器", "处理器型号", "芯片", "芯片型号", "soc"}
)
_REMOVED_WORDS = (
    "高通",
    "qualcomm",
    "联发科",
    "mediatek",
    "海思",
    "hisilicon",
    "华为",
    "移动平台",
    "处理器",
    "芯片",
    "mobileplatform",
    "processor",
    "八核",
)
_TRANSLATED_WORDS = (
    ("snapdragon", "骁龙"),
    ("dimensity", "天玑"),
    ("kirin", "麒麟"),
    ("至尊版", "elite"),
)
_CHINESE_DIGITS = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6"}
_GENERATION_PREFIX = re.compile(r"^第([一二三四五六\d])代(骁龙.+)$")
_PROCESS_NODE = re.compile(r"\(\d+(?:\.\d+)?nm\)")
_NUMERIC_ALIAS = re.compile(r"^(?P<base>.+?)\s*\((?P<alias>[0-9A-Za-z+ ]*\d[0-9A-Za-z+ ]*)\)$")
_NAME_PREFIX = re.compile(r"^\D*")


def chip_name_token(value: str) -> str:
    """Fold one chip name with fixed, declared rules; never approximate or guess."""

    text = unicodedata.normalize("NFKC", value).casefold()
    text = "".join(text.split()).replace("™", "").replace("®", "")
    text = _PROCESS_NODE.sub("", text)
    for word in _REMOVED_WORDS:
        text = text.replace(word, "")
    for source, target in _TRANSLATED_WORDS:
        text = text.replace(source, target)
    generation = _GENERATION_PREFIX.match(text)
    if generation is not None:
        number = _CHINESE_DIGITS.get(generation.group(1), generation.group(1))
        text = f"{generation.group(2)}gen{number}"
    return text


class BenchmarkModel(BaseModel):
    """Strict immutable base for stored benchmark references."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ChipBenchmarkEntry(BenchmarkModel):
    """One ranked chip exactly as the source names it, with its composite score."""

    name: str = Field(min_length=1, max_length=120)
    score: Decimal = Field(gt=0)

    @property
    def match_tokens(self) -> tuple[str, ...]:
        """Return the folded name, plus a declared numeric alias such as 810(6080)."""

        alias = _NUMERIC_ALIAS.match(self.name)
        if alias is None:
            return (chip_name_token(self.name),)
        base = alias.group("base")
        prefix = _NAME_PREFIX.match(base)
        assert prefix is not None  # r"^\D*" always matches, possibly empty.
        return (
            chip_name_token(base),
            chip_name_token(f"{prefix.group(0)}{alias.group('alias')}"),
        )


class ChipBenchmarkReference(BenchmarkModel):
    """A dated snapshot of one public ranking, with its method kept for the report."""

    source_url: HttpUrl
    source_title: str = Field(min_length=1, max_length=500)
    method: str = Field(min_length=1, max_length=500)
    captured_at: AwareDatetime
    entries: tuple[ChipBenchmarkEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def entry_names_are_unique(self) -> Self:
        names = [item.name for item in self.entries]
        if len(names) != len(set(names)):
            raise ValueError("benchmark entry names must be unique")
        return self

    def lookup(self, chip_text: str) -> ChipBenchmarkEntry | None:
        """Return the only entry whose folded name equals the folded chip text."""

        token = chip_name_token(chip_text)
        matches = [item for item in self.entries if token in item.match_tokens]
        if len({item.score for item in matches}) != 1:
            return None
        return min(matches, key=lambda item: item.name)


class ChipBenchmarkEvidenceProvider:
    """Turn uniquely matched platform chip specifications into independent evidence."""

    def __init__(self, reference: ChipBenchmarkReference) -> None:
        self._reference = reference

    def evidence_for(
        self,
        *,
        request_id: UUID,
        product: Product,
        platform_evidence: tuple[Evidence, ...],
    ) -> tuple[Evidence, ...]:
        """Return one evidence row per distinct matched chip; unmatched chips add nothing."""

        matched: dict[str, tuple[ChipBenchmarkEntry, str]] = {}
        for item in platform_evidence:
            key = item.field_path.removeprefix("specifications.")
            if (
                item.request_id != request_id
                or item.subject_type is not EvidenceSubjectType.PRODUCT
                or item.subject_id != product.id
                or not item.field_path.startswith("specifications.")
                or "".join(key.casefold().split()) not in CHIP_SPECIFICATION_KEYS
                or not isinstance(item.observed_value, str)
            ):
                continue
            entry = self._reference.lookup(item.observed_value)
            if entry is not None:
                matched.setdefault(entry.name, (entry, item.observed_value))

        return tuple(
            Evidence(
                request_id=request_id,
                subject_type=EvidenceSubjectType.PRODUCT,
                subject_id=product.id,
                field_path=CHIP_PERFORMANCE_FIELD,
                source_type=EvidenceSourceType.INDEPENDENT_REVIEW,
                source_url=self._reference.source_url,
                source_title=self._reference.source_title,
                captured_at=self._reference.captured_at,
                observed_value=f"{format(entry.score, 'f')} {CHIP_PERFORMANCE_UNIT}",
                notes=(
                    f"平台芯片：{raw_chip[:200]}；排行名称：{entry.name}；"
                    f"方法：{self._reference.method}"
                ),
            )
            for entry, raw_chip in (matched[name] for name in sorted(matched))
        )


BASELINE_SCORE = Decimal("100")


def suggested_chip_criterion(reference: ChipBenchmarkReference) -> dict[str, str]:
    """A soft "higher is better" criterion: the ranking baseline earns half credit, the top chip
    earns full credit, and nothing is excluded. The user still confirms it before use."""

    top = max(item.score for item in reference.entries)
    return {
        "key": "chip_performance",
        "minimum": format(BASELINE_SCORE, "f"),
        "preferred": format(max(top, BASELINE_SCORE), "f"),
        "unit": CHIP_PERFORMANCE_UNIT,
    }
