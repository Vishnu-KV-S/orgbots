"""An organization's policies, as a bot's turn reads them (`domain.policies`).

Read-only: admins change policies through `runtime.runtime.enterprise`. A turn needs two
things from here — whether Auto Review is required (`BotService.get` applies it), and a
note for the prompt saying what the bot's environment is: which hosts it may reach, and
which team secrets its commands can read, by name. Never a secret's value.
"""

from __future__ import annotations

import uuid

from runtime.domain.policies import Policy
from runtime.persistence.uow import UnitOfWorkFactory


class PolicyService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def policy(self, organization_id: uuid.UUID) -> Policy:
        async with self._uow() as uow:
            return await uow.policies.get(organization_id)

    async def for_prompt(self, organization_id: uuid.UUID) -> str:
        async with self._uow() as uow:
            policy = await uow.policies.get(organization_id)
            names = [s.name for s in await uow.team_secrets.for_organization(organization_id)]
        lines: list[str] = []
        if policy.network == "allowlist":
            hosts = ", ".join(policy.allowed_hosts) or "none"
            lines.append(
                "Your organization limits the web: your browser can reach only these hosts "
                f"and their subdomains — {hosts}. Anything else is refused; say so rather "
                "than looking for a way around it. Commands you run have no network."
            )
        if names:
            lines.append(
                "Team secrets your sandboxed commands can read as environment variables: "
                + ", ".join(f"${n}" for n in names)
                + ". Use them by name (e.g. in a header); never print one — it is redacted "
                "if you do."
            )
        return "\n".join(lines)
