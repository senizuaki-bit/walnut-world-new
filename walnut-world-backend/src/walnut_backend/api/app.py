"""FastAPI application factory and lifespan dependency injection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from walnut_backend.adapters.postgres.agent_sessions import PostgresAgentSessionStore
from walnut_backend.adapters.postgres.agent_turns import PostgresAgentTurnStore
from walnut_backend.adapters.postgres.client_events import PostgresClientEventStore
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.event_store import PostgresEventStore
from walnut_backend.adapters.postgres.feishu_learning import PostgresFeishuLearningStore
from walnut_backend.adapters.postgres.product_content import PostgresProductContentStore
from walnut_backend.adapters.postgres.product_drafts import PostgresProductDraftStore
from walnut_backend.adapters.postgres.product_interactions import PostgresProductInteractionStore
from walnut_backend.adapters.postgres.product_workspaces import PostgresProductWorkspaceStore
from walnut_backend.adapters.postgres.run_evidence import PostgresRunEvidenceStore
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.skill_activations import PostgresSkillActivationStore
from walnut_backend.adapters.postgres.skill_builds import PostgresSkillBuildStore
from walnut_backend.adapters.postgres.student_bootstrap import PostgresStudentBootstrapReader
from walnut_backend.adapters.postgres.workflow_jobs import PostgresWorkflowJobStore
from walnut_backend.adapters.postgres.world import PostgresWorld
from walnut_backend.adapters.postgres.world_presentation import PostgresWorldPresentation
from walnut_backend.api.middleware import TransportMiddleware, WebSocketTransportMiddleware
from walnut_backend.api.realtime import router as realtime_router
from walnut_backend.api.routes.agent_sessions import router as agent_sessions_router
from walnut_backend.api.routes.agent_turns import router as agent_turns_router
from walnut_backend.api.routes.client_events import router as client_events_router
from walnut_backend.api.routes.feishu_learning import router as feishu_learning_router
from walnut_backend.api.routes.feishu_mcp import router as feishu_mcp_router
from walnut_backend.api.routes.game_reads import router as game_reads_router
from walnut_backend.api.routes.game_reads import (
    world_presentation_router as world_presentation_reads_router,
)
from walnut_backend.api.routes.product_capabilities import router as product_capabilities_router
from walnut_backend.api.routes.product_content import router as product_content_router
from walnut_backend.api.routes.product_drafts import router as product_drafts_router
from walnut_backend.api.routes.product_interactions import (
    patch_decision_router as product_patch_decision_router,
)
from walnut_backend.api.routes.product_interactions import router as product_interactions_router
from walnut_backend.api.routes.product_workspaces import router as product_workspaces_router
from walnut_backend.api.routes.skill_activations import router as skill_activations_router
from walnut_backend.api.routes.skill_builds import router as skill_builds_router
from walnut_backend.api.routes.student_bootstrap import router as student_bootstrap_router
from walnut_backend.application.feishu.learning_queries import FeishuLearningQueries
from walnut_backend.application.game.agent_sessions import AgentSessions
from walnut_backend.application.game.agent_turns import AgentTurns
from walnut_backend.application.game.client_events import ClientEvents
from walnut_backend.application.game.queries import GameQueries
from walnut_backend.application.game.skill_activations import SkillActivations
from walnut_backend.application.game.skill_builds import SkillBuildCommands
from walnut_backend.application.game.student_bootstrap import StudentBootstrapQueries
from walnut_backend.application.product.content import ProductContent
from walnut_backend.application.product.drafts import ProductDrafts
from walnut_backend.application.product.interactions import ProductInteractions
from walnut_backend.application.product.workspaces import ProductWorkspaces
from walnut_backend.application.realtime.subscription import RealtimeSubscriptions
from walnut_backend.bootstrap import ContractRelease, Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create an adapter-only application; domain routes attach ports in later tasks."""
    resolved_settings = settings or Settings.from_env()
    contract_release = ContractRelease(resolved_settings)
    error_catalog = contract_release.error_catalog()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sessions = create_session_factory(resolved_settings.database_url)
        app.state.settings = resolved_settings
        app.state.contract_release = contract_release
        app.state.error_catalog = error_catalog
        command_store = PostgresCommandStore(sessions)
        workflow_jobs = PostgresWorkflowJobStore(sessions)
        app.state.workflow_jobs = workflow_jobs
        app.state.game_queries = GameQueries(
            command_store,
            PostgresWorld(sessions),
            PostgresEventStore(sessions),
            PostgresWorldPresentation(sessions),
            PostgresRunEvidenceStore(sessions),
            realtime_wss_enabled=resolved_settings.realtime_wss_enabled,
            client_event_batch_enabled=resolved_settings.client_event_batch_enabled,
            public_realtime_url=resolved_settings.public_realtime_url,
        )
        app.state.student_bootstrap_queries = StudentBootstrapQueries(
            PostgresStudentBootstrapReader(sessions)
        )
        app.state.feishu_learning_queries = FeishuLearningQueries(
            PostgresFeishuLearningStore(
                sessions,
                pseudonym_secret=resolved_settings.resolved_feishu_pseudonym_secret(),
            ),
            pseudonym_secret=resolved_settings.resolved_feishu_pseudonym_secret(),
        )
        app.state.skill_build_commands = SkillBuildCommands(
            PostgresSkillBuildStore(sessions, command_store, workflow_jobs)
        )
        app.state.skill_activations = SkillActivations(
            PostgresSkillActivationStore(sessions, command_store, workflow_jobs)
        )
        app.state.agent_sessions = AgentSessions(
            PostgresAgentSessionStore(sessions, command_store, workflow_jobs)
        )
        app.state.agent_turns = AgentTurns(
            PostgresAgentTurnStore(sessions, command_store, workflow_jobs),
            skill_patch_enabled=resolved_settings.skill_patch_enabled,
        )
        if resolved_settings.client_event_batch_enabled:
            app.state.client_events = ClientEvents(
                PostgresClientEventStore(sessions, command_store)
            )
        app.state.product_drafts = ProductDrafts(PostgresProductDraftStore(sessions))
        app.state.product_content = ProductContent(PostgresProductContentStore(sessions))
        app.state.product_interactions = ProductInteractions(PostgresProductInteractionStore(sessions))
        app.state.product_workspaces = ProductWorkspaces(PostgresProductWorkspaceStore(sessions))
        if resolved_settings.realtime_wss_enabled:
            app.state.realtime_subscriptions = RealtimeSubscriptions(
                PostgresWorld(sessions), PostgresEventStore(sessions)
            )
        try:
            yield
        finally:
            await sessions.kw["bind"].dispose()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        TransportMiddleware,
        settings=resolved_settings,
        error_catalog=error_catalog,
    )
    app.add_middleware(
        WebSocketTransportMiddleware,
        settings=resolved_settings,
        error_catalog=error_catalog,
    )
    app.include_router(game_reads_router)
    if resolved_settings.world_presentation_enabled:
        app.include_router(world_presentation_reads_router)
    app.include_router(student_bootstrap_router)
    app.include_router(feishu_learning_router)
    app.include_router(feishu_mcp_router)
    app.include_router(skill_builds_router)
    app.include_router(skill_activations_router)
    app.include_router(agent_sessions_router)
    app.include_router(agent_turns_router)
    if resolved_settings.client_event_batch_enabled:
        app.include_router(client_events_router)
    app.include_router(product_drafts_router)
    app.include_router(product_content_router)
    app.include_router(product_capabilities_router)
    app.include_router(product_interactions_router)
    if resolved_settings.skill_patch_enabled:
        app.include_router(product_patch_decision_router)
    app.include_router(product_workspaces_router)
    if resolved_settings.realtime_wss_enabled:
        app.include_router(realtime_router)
    return app
