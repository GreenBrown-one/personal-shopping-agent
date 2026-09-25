"""Explicit outer-layer composition roots for runnable shopping capabilities."""

from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.automation import (
    ShoppingDecisionPipelineService,
    ShoppingWorkflowService,
)
from personal_shopping_agent.domain import Product
from personal_shopping_agent.infrastructure.storage import (
    SQLiteCandidateScoringUnitOfWork,
    SQLiteEvidenceCrossCheckUnitOfWork,
    SQLiteOfferIngestionUnitOfWork,
    SQLiteSearchDetailUnitOfWork,
    SQLiteShoppingReportRepository,
    SQLiteShoppingReportUnitOfWork,
    SQLiteSpecificationNormalizationUnitOfWork,
    SQLiteWorkflowRepository,
)
from personal_shopping_agent.presentation import (
    CandidateScoringService,
    ShoppingReportService,
)
from personal_shopping_agent.sourcing import (
    CandidateDiscoveryService,
    EvidenceCrossCheckService,
    IndependentEvidenceProvider,
    OfferIngestionService,
    OfficialEvidenceProvider,
    OfficialProductObservation,
    PageCollector,
    ProductDetailService,
    SearchDetailCollectionService,
    SpecificationNormalizationService,
)
from personal_shopping_agent.sourcing.browser import NavigationPolicy
from personal_shopping_agent.sourcing.platforms import JDDetailAdapter, JDSearchAdapter
from personal_shopping_agent.sourcing.platforms.jd import (
    JD_HOME_HOST,
    JD_ITEM_HOST,
    JD_SEARCH_HOST,
    JD_SIGN_IN_HOST,
    JD_SUBRESOURCE_DOMAINS,
)
from personal_shopping_agent.sourcing.platforms.socpk import SOCPK_DOMAIN, SOCPK_HOST


def create_jd_collection_policy() -> NavigationPolicy:
    """Pages: exact search/item hosts. Resources: JD's own domains. Login redirects stop."""

    return NavigationPolicy(
        (JD_SEARCH_HOST, JD_ITEM_HOST),
        subresource_domains=JD_SUBRESOURCE_DOMAINS,
        sign_in_hosts=(JD_SIGN_IN_HOST,),
    )


def create_jd_sign_in_policy() -> NavigationPolicy:
    """Allow the user-driven sign-in page and JD's own post-login landing pages only."""

    return NavigationPolicy(
        (JD_SIGN_IN_HOST, JD_HOME_HOST, JD_SEARCH_HOST, JD_ITEM_HOST),
        subresource_domains=JD_SUBRESOURCE_DOMAINS,
    )


def create_benchmark_policy() -> NavigationPolicy:
    """Only the public SOCPK ranking page and its own same-domain resources."""

    return NavigationPolicy((SOCPK_HOST, SOCPK_DOMAIN), subresource_domains=(SOCPK_DOMAIN,))


class NoOfficialEvidenceProvider:
    """Explicit offline provider that records official sources as missing, never guessed."""

    async def collect(self, product: Product) -> tuple[OfficialProductObservation, ...]:
        del product
        return ()


def create_jd_pipeline_service(
    session_factory: sessionmaker[Session],
    page_collector: PageCollector,
    official_evidence_provider: OfficialEvidenceProvider,
    *,
    independent_providers: tuple[IndependentEvidenceProvider, ...] = (),
) -> ShoppingDecisionPipelineService:
    """Compose every existing JD, decision, storage, and report use case around explicit ports."""

    workflow_service = ShoppingWorkflowService(SQLiteWorkflowRepository(session_factory))
    collection_service = SearchDetailCollectionService(
        CandidateDiscoveryService(page_collector, JDSearchAdapter()),
        ProductDetailService(page_collector, JDDetailAdapter()),
        lambda: SQLiteSearchDetailUnitOfWork(session_factory),
    )
    return ShoppingDecisionPipelineService(
        workflow_service,
        collection_service,
        OfferIngestionService(lambda: SQLiteOfferIngestionUnitOfWork(session_factory)),
        EvidenceCrossCheckService(
            lambda: SQLiteEvidenceCrossCheckUnitOfWork(session_factory),
            official_evidence_provider,
            independent_providers=independent_providers,
        ),
        SpecificationNormalizationService(
            lambda: SQLiteSpecificationNormalizationUnitOfWork(session_factory)
        ),
        CandidateScoringService(lambda: SQLiteCandidateScoringUnitOfWork(session_factory)),
        ShoppingReportService(lambda: SQLiteShoppingReportUnitOfWork(session_factory)),
        SQLiteShoppingReportRepository(session_factory),
    )
