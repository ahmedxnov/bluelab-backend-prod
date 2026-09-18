"""The Phase 2 E-1 consumer on the closed ``dispatch_email`` lane."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.email import EmailTransport, create_email_transport
from bluelab.adapters.object_store import ObjectStore, create_object_store
from bluelab.adapters.secrets import (
    SecretUnsealer,
    create_delivery_unsealer,
)
from bluelab.modules.identity import service as identity_service
from bluelab.notifications import dispatcher
from bluelab.platform.config import Settings
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.compat import JobEnvelope
from bluelab.platform.queue.runtime import (
    JobRegistration,
    NonRetryableJobFailure,
)


def registration(
    settings: Settings,
    *,
    transport: EmailTransport | None = None,
    unsealer: SecretUnsealer | None = None,
) -> JobRegistration:
    """Bind configured capabilities once for the life of the worker process."""
    resolved_transport = transport or create_email_transport(settings)
    resolved_unsealer = unsealer or create_delivery_unsealer(settings)
    resolved_object_store: ObjectStore | None = None

    async def resolve_recipient(
        session: AsyncSession, *, account_id: UUID, org_id: UUID
    ) -> dispatcher.E1Recipient | None:
        return await identity_service.resolve_e1_recipient(
            session, account_id=account_id, org_id=org_id
        )

    async def handle(session: AsyncSession, job: JobEnvelope) -> None:
        nonlocal resolved_object_store
        email_send_id = UUID(str(job.args["email_send_id"]))
        try:
            kind = (await session.execute(text("select kind from email_send where id=:id"), {"id": email_send_id})).scalar_one_or_none()
            if kind == "E1_credentials":
                await dispatcher.dispatch_e1(session, email_send_id=email_send_id, unsealer=resolved_unsealer, transport=resolved_transport, recipient_resolver=resolve_recipient, sender=str(settings.email_sender), public_app_url=settings.public_app_url)
            elif kind == "E2_invite":
                await dispatcher.dispatch_e2(session, email_send_id=email_send_id, unsealer=resolved_unsealer, transport=resolved_transport, sender=str(settings.email_sender), public_app_url=settings.public_app_url)
            elif kind in {"E3_candidate_report", "E4_shortlist", "E5_completion"}:
                if resolved_object_store is None:
                    resolved_object_store = create_object_store(settings)
                await dispatcher.dispatch_e3_or_e4(session, email_send_id=email_send_id, transport=resolved_transport, sender=str(settings.email_sender), object_store=resolved_object_store, public_app_url=settings.public_app_url)
            elif kind is not None:
                raise dispatcher.TerminalDispatchError("email kind has no dispatcher")
        except dispatcher.TerminalDispatchError:
            raise NonRetryableJobFailure("email cannot be dispatched") from None

    async def exhausted(session: AsyncSession, job: JobEnvelope) -> None:
        await dispatcher.mark_dispatch_failed(
            session,
            email_send_id=UUID(str(job.args["email_send_id"])),
        )

    return JobRegistration(
        lane=Lane.DISPATCH_EMAIL,
        handler=handle,
        on_exhausted=exhausted,
    )
