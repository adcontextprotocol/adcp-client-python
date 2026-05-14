"""Dev fixtures — seed two tenants + buyer agents + accounts for local
end-to-end testing of the translator pattern.

Each seeded :class:`Account` carries upstream-routing (``network_code`` +
``advertiser_id``) on the ``ext`` JSON column. The platform reads these
to scope upstream calls to the right tenant on the JS mock-server.

::

    # Boot the upstream first
    npx -y -p @adcp/client@latest \\
        adcp mock-server sales-guaranteed --port 4503 --api-key test-key &

    # Then seed
    docker compose up -d postgres
    DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp \\
      python -m examples.v3_reference_seller.seed

After seeding, hit:

* ``http://acme.localhost:3001/.well-known/agent.json``  (A2A)
* ``http://acme.localhost:3001/mcp``                     (MCP)

with a buyer agent's signed-request or bearer credential matching
the seeded ``api_key_id``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.models import Account, BuyerAgent, Tenant


def _run_alembic_upgrade_head(db_url: str) -> None:
    """Run ``alembic upgrade head`` against ``db_url``.

    Mirrors the production entrypoint in ``migrate.py`` so seed runs
    against the same schema-evolution path as production — column
    renames and type changes propagate instead of being silently
    skipped by ``Base.metadata.create_all``.
    """
    from alembic import command
    from alembic.config import Config

    ini_path = Path(__file__).parent / "alembic.ini"
    # env.py reads DATABASE_URL from os.environ; export so the alembic
    # script picks up the same URL the seed script connects with.
    os.environ["DATABASE_URL"] = db_url
    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "head")


async def main() -> None:
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres@localhost/adcp",
    )
    # Run migrations in a thread — alembic.command.upgrade is sync and
    # opens its own engine, so calling it directly from an async context
    # is safe via asyncio.to_thread.
    await asyncio.to_thread(_run_alembic_upgrade_head, db_url)
    engine = create_async_engine(db_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async with sm() as session:
        async with session.begin():
            session.add_all(
                [
                    Tenant(id="t_acme", host="acme.localhost", display_name="Acme Publishing"),
                    Tenant(id="t_beta", host="beta.localhost", display_name="Beta Network"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    BuyerAgent(
                        id="ba_acme_signed",
                        tenant_id="t_acme",
                        agent_url="https://signed-buyer.example/",
                        display_name="Signed Buyer",
                        status="active",
                        billing_capabilities=["operator", "agent"],
                        api_key_id=None,
                    ),
                    BuyerAgent(
                        id="ba_acme_bearer",
                        tenant_id="t_acme",
                        agent_url="https://bearer-buyer.example/",
                        display_name="Bearer Buyer",
                        status="active",
                        billing_capabilities=["operator"],
                        api_key_id="dev-bearer-token-acme-1",
                    ),
                    BuyerAgent(
                        id="ba_beta_suspended",
                        tenant_id="t_beta",
                        agent_url="https://suspended.example/",
                        display_name="Suspended Buyer",
                        status="suspended",
                        billing_capabilities=["operator"],
                    ),
                ]
            )
            await session.flush()
            # Translator-pattern routing: each account.ext maps to
            # an upstream (network_code, advertiser_id) pair. The mock-
            # server's seeded networks are net_premium_us, net_premium_uk,
            # net_acmeoutdoor, net_pinnacle. The advertiser_id values
            # are seeded in the mock's seed-data.ts.
            session.add_all(
                [
                    Account(
                        id="a_acme_1",
                        tenant_id="t_acme",
                        buyer_agent_id="ba_acme_signed",
                        account_id="signed-buyer-main",
                        name="Signed Buyer — Main",
                        status="active",
                        billing="operator",
                        ext={
                            "network_code": "net_premium_us",
                            "advertiser_id": "adv_volta_motors",
                        },
                    ),
                    Account(
                        id="a_acme_2",
                        tenant_id="t_acme",
                        buyer_agent_id="ba_acme_bearer",
                        account_id="bearer-buyer-main",
                        name="Bearer Buyer — Main",
                        status="active",
                        billing="operator",
                        ext={
                            "network_code": "net_premium_us",
                            "advertiser_id": "adv_volta_motors",
                        },
                    ),
                ]
            )

    print("Seeded: 2 tenants, 3 buyer agents, 2 accounts.")
    print("Each account routes to upstream network=net_premium_us advertiser=adv_volta_motors.")
    print("Hit: http://acme.localhost:3001/.well-known/agent.json")
    print("Hit: http://acme.localhost:3001/mcp")


if __name__ == "__main__":
    asyncio.run(main())
