"""Chip names match a dated benchmark reference exactly; nothing is guessed."""

# ruff: noqa: RUF001 -- platform chip text intentionally uses full-width punctuation.

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import HttpUrl, ValidationError

from personal_shopping_agent.domain import (
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Product,
    measurement_definition,
    normalize_measurement,
)
from personal_shopping_agent.sourcing import (
    CHIP_PERFORMANCE_FIELD,
    ChipBenchmarkEntry,
    ChipBenchmarkEvidenceProvider,
    ChipBenchmarkReference,
    chip_name_token,
)
from personal_shopping_agent.sourcing.benchmarks import suggested_chip_criterion
from personal_shopping_agent.sourcing.browser import BrowserSnapshot
from personal_shopping_agent.sourcing.platforms.socpk import (
    SOCPK_DEFAULT_METHOD,
    SOCPK_OVERALL_URL,
    BenchmarkPageParseError,
    SocpkRankingParser,
)

CAPTURED_AT = datetime(2026, 9, 1, tzinfo=UTC)
FIXTURE = Path(__file__).parents[2] / "fixtures" / "socpk" / "allperf_rendered.html"


def reference(*entries: tuple[str, str]) -> ChipBenchmarkReference:
    return ChipBenchmarkReference(
        source_url=HttpUrl(SOCPK_OVERALL_URL),
        source_title="极客湾 SOCPK",
        method="CPU 70% / GPU 30%",
        captured_at=CAPTURED_AT,
        entries=tuple(
            ChipBenchmarkEntry(name=name, score=Decimal(score)) for name, score in entries
        ),
    )


def snapshot(html: str, *, url: str = SOCPK_OVERALL_URL) -> BrowserSnapshot:
    return BrowserSnapshot.model_validate(
        {
            "requested_url": url,
            "final_url": url,
            "status_code": 200,
            "title": "综合性能排行",
            "html": html,
            "captured_at": CAPTURED_AT,
        }
    )


@pytest.mark.parametrize(
    ("platform_text", "ranking_name"),
    [
        ("第三代骁龙8移动平台", "骁龙 8 Gen3"),
        ("高通 Snapdragon 8 Gen 3", "骁龙 8 Gen3"),
        ("高通骁龙8Gen3 八核", "骁龙 8 Gen3"),
        ("骁龙8 Gen3（4nm）", "骁龙 8 Gen3"),
        ("第三代骁龙8s", "骁龙 8s Gen3"),
        ("第五代骁龙8至尊版", "骁龙 8 Elite Gen5"),
        ("联发科 Dimensity 9400+", "天玑 9400+"),
        ("华为麒麟9000S", "麒麟 9000S"),
    ],
)
def test_declared_folding_rules_align_platform_and_ranking_names(
    platform_text: str, ranking_name: str
) -> None:
    assert chip_name_token(platform_text) == chip_name_token(ranking_name)


def test_near_names_are_never_treated_as_equal() -> None:
    assert chip_name_token("骁龙8 Gen3") != chip_name_token("骁龙8s Gen3")
    assert chip_name_token("天玑9400") != chip_name_token("天玑9400+")
    assert chip_name_token("骁龙8 Gen3 领先版") != chip_name_token("骁龙8 Gen3")


def test_numeric_aliases_match_both_names_and_other_parentheses_do_not_split() -> None:
    assert ChipBenchmarkEntry(name="天玑 810(6080)", score=Decimal("60")).match_tokens == (
        "天玑810",
        "天玑6080",
    )
    assert ChipBenchmarkEntry(name="骁龙 8 Gen3(for Galaxy)", score=Decimal("1")).match_tokens == (
        "骁龙8gen3(forgalaxy)",
    )


def test_lookup_requires_one_unambiguous_score() -> None:
    ranking = reference(
        ("骁龙 8 Gen3", "280"),
        ("天玑 810(6080)", "60"),
        ("天玑 6080", "61"),
        ("天玑 9000", "200"),
        ("天玑9000", "200"),
    )

    assert ranking.lookup("第三代骁龙8") == ranking.entries[0]
    assert ranking.lookup("天玑 810") == ranking.entries[1]
    assert ranking.lookup("天玑6080") is None  # two different scores: ambiguous
    found = ranking.lookup("天玑 9000")
    assert found is not None and found.score == Decimal("200")  # same score: consistent
    assert ranking.lookup("骁龙 8 Gen4") is None


def test_reference_rejects_duplicate_names_and_non_positive_scores() -> None:
    with pytest.raises(ValidationError, match="unique"):
        reference(("骁龙 8 Gen3", "280"), ("骁龙 8 Gen3", "281"))
    with pytest.raises(ValidationError):
        reference(("骁龙 8 Gen3", "0"))


def _platform(
    request_id: object, product_id: object, field_path: str, value: object, **overrides: object
) -> Evidence:
    values: dict[str, object] = {
        "request_id": request_id,
        "subject_type": EvidenceSubjectType.PRODUCT,
        "subject_id": product_id,
        "field_path": field_path,
        "source_type": EvidenceSourceType.PLATFORM_LISTING,
        "source_url": "https://item.jd.com/1.html",
        "source_title": "JD",
        "captured_at": CAPTURED_AT,
        "observed_value": value,
    }
    values.update(overrides)
    return Evidence.model_validate(values)


def test_provider_emits_one_independent_evidence_per_matched_chip() -> None:
    request_id = uuid4()
    product = Product(brand="B", model="M", category="smartphone", canonical_name="B M")
    other_product = uuid4()
    platform = (
        _platform(request_id, product.id, "specifications.CPU型号", "第三代骁龙8"),
        _platform(request_id, product.id, "specifications.处理器", "高通骁龙8 Gen3"),
        _platform(request_id, product.id, "specifications.SoC", "天玑 9400"),
        _platform(request_id, product.id, "specifications.芯片", "未知芯片 X1"),
        _platform(request_id, product.id, "specifications.电池容量", "骁龙 8 Gen3"),
        _platform(request_id, product.id, "specifications.CPU", True),
        _platform(uuid4(), product.id, "specifications.CPU型号", "天玑 9400"),
        _platform(request_id, other_product, "specifications.CPU型号", "天玑 9400"),
        _platform(request_id, product.id, "title", "骁龙 8 Gen3"),
        _platform(
            request_id,
            product.id,
            "specifications.CPU型号",
            "天玑 9400",
            subject_type=EvidenceSubjectType.OFFER,
        ),
    )
    provider = ChipBenchmarkEvidenceProvider(
        reference(("骁龙 8 Gen3", "280.0"), ("天玑 9400", "330"))
    )

    produced = provider.evidence_for(
        request_id=request_id, product=product, platform_evidence=platform
    )

    assert [item.observed_value for item in produced] == ["330 SOCPK", "280.0 SOCPK"]
    for item in produced:
        assert item.field_path == CHIP_PERFORMANCE_FIELD
        assert item.source_type is EvidenceSourceType.INDEPENDENT_REVIEW
        assert item.request_id == request_id
        assert item.subject_id == product.id
        assert item.captured_at == CAPTURED_AT
        assert str(item.source_url) == SOCPK_OVERALL_URL
    assert produced[1].notes is not None and "第三代骁龙8" in produced[1].notes
    definition = measurement_definition(CHIP_PERFORMANCE_FIELD)
    assert definition is not None and definition.canonical_unit == "SOCPK"
    assert normalize_measurement("280.0 SOCPK", definition) == Decimal("280")


def test_provider_adds_nothing_without_a_matching_chip() -> None:
    request_id = uuid4()
    product = Product(brand="B", model="M", category="smartphone", canonical_name="B M")
    provider = ChipBenchmarkEvidenceProvider(reference(("骁龙 8 Gen3", "280")))

    assert provider.evidence_for(request_id=request_id, product=product, platform_evidence=()) == ()


def test_suggested_criterion_ramps_from_the_baseline_to_the_top_chip() -> None:
    assert suggested_chip_criterion(reference(("A", "280"), ("B", "420.5"))) == {
        "key": "chip_performance",
        "minimum": "100",
        "preferred": "420.5",
        "unit": "SOCPK",
    }
    assert suggested_chip_criterion(reference(("Old", "60")))["preferred"] == "100"


def test_parser_reads_every_rendered_row_and_the_published_method() -> None:
    parsed = SocpkRankingParser().parse(snapshot(FIXTURE.read_text(encoding="utf-8")))

    assert len(parsed.entries) == 22
    assert parsed.entries[0] == ChipBenchmarkEntry(name="骁龙 8 Elite Gen5", score=Decimal("420.0"))
    assert "CPU权重70%" in parsed.method
    assert parsed.captured_at == CAPTURED_AT
    assert parsed.lookup("第三代骁龙8") is not None
    assert parsed.lookup("天玑6080") is not None


def _table(rows: list[tuple[str, str]], *, notice: bool = True) -> str:
    body = "".join(
        f'<tr><td class="socName">{name}</td><td class="logo"></td>'
        f'<td><div class="ratio"><a>{score}</a></div></td></tr>'
        for name, score in rows
    )
    head = '<p id="notice">方法说明</p>' if notice else ""
    return f"<html><body>{head}<table>{body}</table></body></html>"


def test_parser_accepts_repeated_identical_rows_and_defaults_the_method() -> None:
    rows = [(f"芯片 {index}", f"{index + 1}.0") for index in range(20)]
    parsed = SocpkRankingParser().parse(snapshot(_table([*rows, rows[0]], notice=False)))

    assert len(parsed.entries) == 20
    assert parsed.method == SOCPK_DEFAULT_METHOD


@pytest.mark.parametrize(
    ("html", "url", "message"),
    [
        ("<html></html>", "https://example.com/allperf/", "socpk.com"),
        (_table([("芯片 A", "fast")]), SOCPK_OVERALL_URL, "non-numeric"),
        (_table([("芯片 A", "1"), ("芯片 A", "2")]), SOCPK_OVERALL_URL, "twice"),
        (_table([("芯片 A", "1")]), SOCPK_OVERALL_URL, "did not render completely"),
    ],
)
def test_parser_rejects_foreign_partial_or_inconsistent_pages(
    html: str, url: str, message: str
) -> None:
    with pytest.raises(BenchmarkPageParseError, match=message):
        SocpkRankingParser().parse(snapshot(html, url=url))
