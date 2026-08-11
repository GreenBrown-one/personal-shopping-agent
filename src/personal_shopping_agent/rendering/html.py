"""Sandboxed Jinja2 adapter for deterministic, standalone HTML shopping reports."""

import hashlib
from decimal import Decimal

from jinja2 import PackageLoader, StrictUndefined, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

from personal_shopping_agent.application.reporting import (
    REPORT_BUDGET_LABELS,
    REPORT_EXCLUSION_LABELS,
    REPORT_STATUS_LABELS,
    RenderedShoppingReport,
    ReportFormat,
    ShoppingDecisionReport,
)
from personal_shopping_agent.domain import Money

_TEMPLATE_NAME = "shopping_report.html.j2"


def _format_decimal(value: Decimal, places: int = 6) -> str:
    return format(value, f".{places}f")


def _format_money(value: Money) -> str:
    return f"{value.currency} {format(value.amount, 'f')}"


_HTML_ENVIRONMENT = SandboxedEnvironment(
    loader=PackageLoader("personal_shopping_agent", "templates"),
    autoescape=select_autoescape(
        enabled_extensions=("html", "htm", "xml", "j2"),
        default_for_string=True,
        default=True,
    ),
    undefined=StrictUndefined,
    auto_reload=False,
    trim_blocks=True,
    lstrip_blocks=True,
)
_HTML_ENVIRONMENT.filters["decimal"] = _format_decimal
_HTML_ENVIRONMENT.filters["money"] = _format_money


class HtmlShoppingReportRenderer:
    """Render one validated report as self-contained, script-free HTML."""

    def render(self, report: ShoppingDecisionReport) -> RenderedShoppingReport:
        """Create deterministic HTML while relying on Jinja autoescaping for all facts."""

        content = (
            _HTML_ENVIRONMENT.get_template(_TEMPLATE_NAME)
            .render(
                report=report,
                eligible=tuple(item for item in report.candidates if item.score.eligible),
                excluded=tuple(item for item in report.candidates if not item.score.eligible),
                budget_labels=REPORT_BUDGET_LABELS,
                status_labels=REPORT_STATUS_LABELS,
                exclusion_labels=REPORT_EXCLUSION_LABELS,
                zero=Decimal("0"),
            )
            .strip()
        )
        return RenderedShoppingReport(
            report=report,
            format=ReportFormat.HTML,
            content=content,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            rendered_at=report.generated_at,
        )
