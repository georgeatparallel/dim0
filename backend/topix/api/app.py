"""FastAPI application setup."""

import asyncio
import logging

from argparse import ArgumentParser
from contextlib import asynccontextmanager

import uvicorn

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from topix.agents.websearch.parallel import close_client as close_parallel_client
from topix.api.router import (
    ai,
    billing,
    boards,
    chats,
    collab,
    documents,
    files,
    finance,
    mini_app_state,
    sharing,
    subscriptions,
    tools,
    users,
    utils,
)
from topix.collab.agent_bridge import AgentBoardBridge
from topix.collab.room import RoomRegistry
from topix.config.config import Config
from topix.datatypes.stage import StageEnum
from topix.nlp.pipeline.parsing import ParsingPipeline
from topix.setup import setup
from topix.store.chat import ChatStore
from topix.store.collab_oplog import CollabOplogStore
from topix.store.email_verification import EmailVerificationStore
from topix.store.graph import GraphStore
from topix.store.mini_app_state import MiniAppStateStore
from topix.store.password_reset import PasswordResetStore
from topix.store.postgres.pool import create_pool
from topix.store.postgres.schema import apply_schema
from topix.store.redis.store import RedisStore
from topix.store.subscription import SubscriptionStore
from topix.store.user import UserStore
from topix.store.user_billing import UserBillingStore
from topix.utils.logging import logging_config

logging_config()
logger = logging.getLogger(__name__)


def create_app(stage: StageEnum):
    """Create and configure the FastAPI application."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan context manager."""
        # One shared Postgres pool for every store. Per-store pools used to
        # multiply our connection footprint and exhaust Postgres under burst.
        app.pg_pool = await create_pool()

        # Apply idempotent schema so existing self-hosted DBs pick up additive
        # changes (new tables, new columns) without a manual migration step.
        await apply_schema(app.pg_pool)

        # Initialize stores
        app.graph_store = GraphStore()
        await app.graph_store.open(app.pg_pool)
        app.user_store = UserStore()
        await app.user_store.open(app.pg_pool)
        app.chat_store = ChatStore()
        await app.chat_store.open(app.pg_pool)
        app.user_billing_store = UserBillingStore()
        await app.user_billing_store.open(app.pg_pool)
        app.email_verification_store = EmailVerificationStore()
        await app.email_verification_store.open(app.pg_pool)
        app.password_reset_store = PasswordResetStore()
        await app.password_reset_store.open(app.pg_pool)
        app.mini_app_state_store = MiniAppStateStore()
        await app.mini_app_state_store.open(app.pg_pool)
        app.subscription_store = SubscriptionStore()
        await app.subscription_store.open()
        app.parser_pipeline = ParsingPipeline()

        # Initialize Redis
        app.redis_store = RedisStore.from_config()

        # Durable collab op-log (post-office substrate): persists every applied
        # batch and allocates a restart-safe per-board seq (Redis INCR seeded
        # from Postgres). Makes the client's serverSeq ordering survive restarts.
        app.collab_oplog = CollabOplogStore(app.redis_store)
        await app.collab_oplog.open(app.pg_pool)

        # Per-worker collab room registry (in-process; single-worker for v1).
        app.collab_rooms = RoomRegistry()
        # Agent → room bridge: agent tools call this so their edits
        # surface to live peers via `peer-op` (collab-archi §5.3 Phase 2).
        app.agent_board_bridge = AgentBoardBridge(
            graph_store=app.graph_store,
            registry=app.collab_rooms,
            oplog=app.collab_oplog,
        )

        yield

        # Close stores. They no-op the pool close when sharing, then we close
        # the shared pool exactly once at the end.
        await app.graph_store.close()
        await app.user_store.close()
        await app.chat_store.close()
        await app.user_billing_store.close()
        await app.email_verification_store.close()
        await app.password_reset_store.close()
        await app.mini_app_state_store.close()
        await app.subscription_store.close()
        await app.collab_oplog.close()
        # Shared anonymous client for keyless search (module-level, not on app).
        await close_parallel_client()
        # Close Redis
        await app.redis_store.close()
        await app.pg_pool.close()

    # Expose interactive docs and the OpenAPI schema only in local/dev. In
    # staging/prod they leak the full route + payload surface, which makes
    # targeted abuse easier, so disable them outright.
    docs_enabled = stage in (StageEnum.LOCAL, StageEnum.DEV)
    app = FastAPI(
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(ai.router)
    app.include_router(boards.router)
    app.include_router(chats.router)
    app.include_router(collab.router)
    app.include_router(sharing.router)
    app.include_router(tools.router)
    app.include_router(users.router)
    app.include_router(subscriptions.router)
    app.include_router(billing.router)
    app.include_router(mini_app_state.router)
    app.include_router(utils.router)
    app.include_router(finance.router)
    app.include_router(files.router)
    app.include_router(documents.router)

    return app


async def main(args) -> tuple[FastAPI, int]:
    """Run the application entry point."""
    await setup(stage=args.stage, env_filename=args.env_file)

    config: Config = Config.instance()

    app = create_app(stage=args.stage)

    return app, args.port or config.app.settings.port


if __name__ == "__main__":
    args = ArgumentParser(description="Run the Dim0 application.")
    args.add_argument(
        "--stage",
        default=StageEnum.LOCAL,
        help="The stage to run the application in.",
        choices=list(StageEnum)
    )
    args.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to run the application on."
    )
    args.add_argument(
        "--env-file",
        type=str,
        default=".env",
        help="Overridden name to the .env file to load. For example: .env.staging",
    )
    args = args.parse_args()

    app, port = asyncio.run(main(args))

    host = "0.0.0.0"
    logger.info(f"Starting Dim0 API on {host}:{port}...")

    uvicorn.run(app, host=host, port=port, log_level="info")
