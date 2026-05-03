"""Dev fixtures — seed two tenants + buyer agents for local
end-to-end testing.

::

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

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.models import Account, Base, BuyerAgent, Creative, Tenant


async def main() -> None:
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres@localhost/adcp",
    )
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    # Insert in FK-dependency order with explicit flushes so the
    # accounts → buyer_agents → tenants chain commits correctly.
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
                    ),
                    Account(
                        id="a_acme_2",
                        tenant_id="t_acme",
                        buyer_agent_id="ba_acme_bearer",
                        account_id="bearer-buyer-main",
                        name="Bearer Buyer — Main",
                        status="active",
                        billing="operator",
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Creative(
                        id="cr_demo_1",
                        tenant_id="t_acme",
                        account_id="a_acme_1",
                        creative_id="signed-300x250-spring",
                        name="Spring 300x250 Display",
                        format_id={
                            "agent_url": "https://reference.adcp.org",
                            "id": "display_300x250",
                        },
                        status="approved",
                        manifest_json={
                            "creative_id": "signed-300x250-spring",
                            "name": "Spring 300x250 Display",
                            "format_id": {
                                "agent_url": "https://reference.adcp.org",
                                "id": "display_300x250",
                            },
                        },
                    ),
                    Creative(
                        id="cr_demo_2",
                        tenant_id="t_acme",
                        account_id="a_acme_2",
                        creative_id="bearer-video-30s",
                        name="Bearer Buyer Video 30s",
                        format_id={
                            "agent_url": "https://reference.adcp.org",
                            "id": "video_16x9_30s",
                        },
                        status="approved",
                        manifest_json={
                            "creative_id": "bearer-video-30s",
                            "name": "Bearer Buyer Video 30s",
                        },
                    ),
                ]
            )

    print("Seeded: 2 tenants, 3 buyer agents, 2 accounts, 2 creatives.")
    print("Hit: http://acme.localhost:3001/.well-known/agent.json")
    print("Hit: http://acme.localhost:3001/mcp")


if __name__ == "__main__":
    asyncio.run(main())
