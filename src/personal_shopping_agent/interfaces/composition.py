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
    OfferIngestionService,
    OfficialEvidenceProvider,
    OfficialProductObservation,
    PageCollector,
    ProductDetailService,
    SearchDetailCollectionService,
    SpecificationNormalizationService,
)
from personal_shopping_agent.sourcing.platforms import JDDetailAdapter, JDSearchAdapter


class NoOfficialEvidenceProvider:
    """Explicit offline provider that records official sources as missing, never guessed."""

    async def collect(self, product: Product) -> tuple[OfficialProductObservation, ...]:
        del product
        return ()


def create_jd_pipeline_service(
    session_factory: sessionmaker[Session],
    page_collector: PageCollector,
    official_evidence_provider: OfficialEvidenceProvider,
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
        ),
        SpecificationNormalizationService(
            lambda: SQLiteSpecificationNormalizationUnitOfWork(session_factory)
        ),
        CandidateScoringService(lambda: SQLiteCandidateScoringUnitOfWork(session_factory)),
        ShoppingReportService(lambda: SQLiteShoppingReportUnitOfWork(session_factory)),
        SQLiteShoppingReportRepository(session_factory),
    )
