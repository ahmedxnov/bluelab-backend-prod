"""The Phase 2 E-1 consumer on the closed ``dispatch_email`` lane."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.email import EmailTransport, create_email_transport
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

    async def resolve_recipient(
        session: AsyncSession, *, account_id: UUID, org_id: UUID
    ) -> dispatcher.E1Recipient | None:
        return await identity_service.resolve_e1_recipient(
            session, account_id=account_id, org_id=org_id
        )

    async def handle(session: AsyncSession, job: JobEnvelope) -> None:
        email_send_id = UUID(str(job.args["email_send_id"]))
        try:
            await dispatcher.dispatch_e1(
                session,
                email_send_id=email_send_id,
                unsealer=resolved_unsealer,
                transport=resolved_transport,
                recipient_resolver=resolve_recipient,
                sender=str(settings.email_sender),
                public_app_url=settings.public_app_url,
            )
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
