"""Tag @bot on X: connecting the organization's X account, linking people's, and the
poller that turns their tags into tasks. The rules are `domain.x`.

**The poller** reads each connected account's mentions (`gateway.x`) on a timer. The
first read only marks where it is — mentions from before the account was connected are
history, not tasks. After that, oldest first, each post is claimed in `x_mentions`
before anything is done with it, so two pollers, or a retry, never act on one twice:

- a post that carries a live link code (`@AcmeBots link 7KQ2MX`) links its author's X
  account to whoever asked for the code;
- a post from a linked account becomes a message to that person's chosen bot — their
  own bot, never one shared with them — with the post, its parent and quoted posts
  (`domain.x.task_text`); with a token that can post, @AcmeBots replies that it has it;
- anything else is recorded and left alone.

Tokens are sealed with the credential cipher and opened for the call that needs them.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime.domain.members import Member, NotAllowedError
from runtime.domain.x import (
    CONFIRMATION,
    LINK_TTL,
    XError,
    clean_handle,
    link_code_in,
    new_link_code,
    refusal,
    task_text,
)
from runtime.gateway.vault import VaultUnavailableError, load_cipher
from runtime.gateway.x import Mention, XAPIError, XClient
from runtime.observability.logging import get_logger
from runtime.org.audit import record
from runtime.persistence.repositories.bots import BotRow
from runtime.persistence.repositories.x import XAccountRow, XLinkRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bots import BotManager
from runtime.settings import Settings

log = get_logger("runtime.x")

ClientFactory = Callable[[str], XClient]


def _aad(organization_id: uuid.UUID, which: str) -> bytes:
    return f"x-token:{which}:".encode() + organization_id.bytes


@dataclass(frozen=True)
class LinkStatus:
    account: str | None
    """The organization's X handle, or None when no account is connected."""
    link: XLinkRow | None


class XService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
        *,
        client: ClientFactory | None = None,
    ) -> None:
        self._uow = uow_factory
        self._settings = settings
        self._client = client or (lambda token: XClient(token, base=settings.x_api_url))

    @property
    def client(self) -> ClientFactory:
        return self._client

    @property
    def poll_seconds(self) -> float:
        return float(self._settings.x_poll_seconds)

    def open_token(self, account: XAccountRow, which: str) -> str | None:
        key = getattr(account, f"{which}_key_id")
        if key is None:
            return None
        return load_cipher(self._settings).decrypt(
            key,
            getattr(account, f"{which}_nonce"),
            getattr(account, f"{which}_ciphertext"),
            aad=_aad(account.organization_id, which),
        )

    # --- the organization's account ----------------------------------------------------

    async def account(self, organization_id: uuid.UUID) -> XAccountRow | None:
        async with self._uow() as uow:
            return await uow.x.account(organization_id)

    async def connect(
        self,
        actor: Member | None,
        organization_id: uuid.UUID,
        *,
        handle: str,
        read_token: str | None,
        post_token: str | None,
        clear_post: bool,
        enabled: bool,
    ) -> XAccountRow:
        """Check the read token can see the account, then save. Tokens None keep the
        saved ones."""
        if actor is not None and not actor.is_admin:
            raise NotAllowedError("only an owner or an admin can connect the X account")
        handle = clean_handle(handle)
        existing = await self.account(organization_id)
        token = read_token or (self.open_token(existing, "read") if existing else None)
        if not token:
            raise XError("the read token is needed the first time")
        try:
            user_id, username = await self._client(token).user(handle)
        except XAPIError as exc:
            raise XError(str(exc)) from exc
        cipher = load_cipher(self._settings)
        read = cipher.encrypt(read_token, aad=_aad(organization_id, "read")) if read_token else None
        post = cipher.encrypt(post_token, aad=_aad(organization_id, "post")) if post_token else None
        async with self._uow.transaction() as uow:
            await uow.x.save_account(
                organization_id,
                handle=username,
                x_user_id=user_id,
                read=read,
                post=post,
                clear_post=clear_post,
                enabled=enabled,
            )
            await record(
                uow,
                organization_id,
                "x.connected",
                actor=actor,
                target=f"@{username}",
                detail={"can_reply": bool(post_token) or bool(existing and existing.post_key_id)},
            )
            row = await uow.x.account(organization_id)
        assert row is not None
        return row

    async def disconnect(self, actor: Member | None, organization_id: uuid.UUID) -> None:
        if actor is not None and not actor.is_admin:
            raise NotAllowedError("only an owner or an admin can disconnect the X account")
        async with self._uow.transaction() as uow:
            await uow.x.delete_account(organization_id)
            await record(uow, organization_id, "x.disconnected", actor=actor)

    # --- a person's link -----------------------------------------------------------------

    async def status(self, organization_id: uuid.UUID, member: Member | None) -> LinkStatus:
        async with self._uow() as uow:
            account = await uow.x.account(organization_id)
            link = await uow.x.link_for(organization_id, member.id if member else None)
        return LinkStatus(account=account.handle if account else None, link=link)

    async def new_code(self, organization_id: uuid.UUID, member: Member | None) -> str:
        async with self._uow.transaction() as uow:
            if await uow.x.account(organization_id) is None:
                raise XError("your organization hasn't connected an X account yet")
            code = new_link_code()
            await uow.x.add_code(
                code,
                organization_id,
                member.id if member else None,
                dt.datetime.now(dt.UTC) + LINK_TTL,
            )
        return code

    async def choose_bot(
        self, organization_id: uuid.UUID, member: Member | None, bot: BotRow
    ) -> None:
        """A tag starts the person's own bot — not one a teammate shared with them."""
        if bot.organization_id != organization_id or (
            member is not None and bot.owner_member_id != member.id
        ):
            raise XError("choose one of your own bots")
        async with self._uow.transaction() as uow:
            link = await uow.x.link_for(organization_id, member.id if member else None)
            if link is None:
                raise XError("link your X account first")
            await uow.x.set_bot(link.id, bot.id)

    async def unlink(self, organization_id: uuid.UUID, member: Member | None) -> None:
        async with self._uow.transaction() as uow:
            if await uow.x.unlink(organization_id, member.id if member else None):
                await record(uow, organization_id, "x.unlinked", actor=member)


@dataclass
class PollTick:
    accounts: int = 0
    tasks: int = 0
    linked: int = 0
    ignored: int = 0
    failed: int = 0


class XPoller:
    def __init__(self, uow_factory: UnitOfWorkFactory, bots: BotManager, service: XService) -> None:
        self._uow = uow_factory
        self._bots = bots
        self._service = service
        self._client: ClientFactory = service.client
        self._every = service.poll_seconds
        self._stopping = asyncio.Event()

    async def tick(self) -> PollTick:
        out = PollTick()
        async with self._uow() as uow:
            accounts = await uow.x.enabled_accounts()
        for account in accounts:
            out.accounts += 1
            try:
                await self._poll(account, out)
            except (XAPIError, VaultUnavailableError) as exc:
                out.failed += 1
                async with self._uow.transaction() as uow:
                    await uow.x.polled(
                        account.organization_id, since_id=None, error=str(exc).splitlines()[0]
                    )
                log.warning("x.poll_failed", organization_id=str(account.organization_id))
        return out

    async def _poll(self, account: XAccountRow, out: PollTick) -> None:
        token = self._service.open_token(account, "read")
        assert token is not None
        mentions = await self._client(token).mentions(account.x_user_id, account.since_id)
        newest = max((m.post.id for m in mentions), key=int, default=None)
        if account.since_id is not None:
            poster = self._service.open_token(account, "post")
            for mention in sorted(mentions, key=lambda m: int(m.post.id)):
                await self._handle(account, mention, poster, out)
        async with self._uow.transaction() as uow:
            await uow.x.polled(
                account.organization_id, since_id=newest or account.since_id or "0", error=""
            )

    async def _handle(
        self, account: XAccountRow, mention: Mention, poster: str | None, out: PollTick
    ) -> None:
        post = mention.post
        org = account.organization_id

        async def note(outcome: str, why: str = "", **kw: Any) -> bool:
            async with self._uow.transaction() as uow:
                return await uow.x.record(
                    post.id,
                    org,
                    author_id=post.author_id,
                    author_handle=post.author_handle,
                    outcome=outcome,
                    note=why,
                    **kw,
                )

        if post.author_id == account.x_user_id:
            await note("own")
            return
        code = link_code_in(post.text)
        if code is not None:
            async with self._uow.transaction() as uow:
                found, member_id = await uow.x.take_code(org, code)
                if found and await uow.x.record(
                    post.id,
                    org,
                    author_id=post.author_id,
                    author_handle=post.author_handle,
                    outcome="linked",
                ):
                    await uow.x.link(
                        org, member_id, x_user_id=post.author_id, handle=post.author_handle
                    )
                    await record(
                        uow,
                        org,
                        "x.linked",
                        actor_label=f"@{post.author_handle}",
                        target=f"@{post.author_handle}",
                    )
                    out.linked += 1
                    return
        async with self._uow() as uow:
            link = await uow.x.link_by_account(org, post.author_id)
        if link is None:
            out.ignored += 1
            await note("ignored", "not linked")
            return
        refused = refusal(post)
        if refused:
            out.ignored += 1
            await note("refused", refused)
            return
        bot = await self._bot_for(org, link)
        if bot is None:
            out.ignored += 1
            await note("no_bot", "no bot of their own to give it to")
            return
        if not await note("started", bot_id=bot.id):
            return
        payload: dict[str, Any] = {"x": {"post": post.url, "author": post.author_handle}}
        run_input: dict[str, Any] = {}
        if link.member_id is not None:
            async with self._uow() as uow:
                member = await uow.members.get(link.member_id)
            if member is not None:
                payload["from"] = {"member_id": str(member.id), "name": member.name or member.email}
                run_input["member_id"] = str(member.id)
        await self._bots.send(
            bot.id,
            task_text(post, account.handle, parent=mention.parent, quoted=mention.quoted),
            payload=payload,
            run_input=run_input or None,
        )
        out.tasks += 1
        if poster:
            with contextlib.suppress(XAPIError):
                await self._client(poster).reply(CONFIRMATION, post.id)

    async def _bot_for(self, organization_id: uuid.UUID, link: XLinkRow) -> BotRow | None:
        async with self._uow() as uow:
            if link.bot_id is not None:
                chosen = await uow.bots.get(link.bot_id)
                if chosen is not None and chosen.owner_member_id == link.member_id:
                    return chosen
            own = [
                b
                for b in await uow.bots.list_for(organization_id)  # type: ignore[arg-type]
                if b.parent_bot_id is None and b.owner_member_id == link.member_id
            ]
        return own[0] if own else None

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("x.tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._every)

    def stop(self) -> None:
        self._stopping.set()
