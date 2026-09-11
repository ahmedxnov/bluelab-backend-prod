"""The closed five-template registry and Phase 2's safe E-1 renderer.

Later phases add the contract variables for E-2 through E-5 when their owning
features can resolve them. The registry is already closed here: an unknown kind
cannot be rendered or dispatched without an explicit scope change.
"""

from __future__ import annotations

import html
import unicodedata
from enum import StrEnum
from urllib.parse import quote
from uuid import UUID

from bluelab.adapters.email import OutboundEmail, validate_address

_FORMAT_CONTROLS = frozenset(
    {
        "\u200b",
        "\u200c",
        "\u200d",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
        "\ufeff",
    }
)


class EmailKind(StrEnum):
    """The complete v1 inventory. There is no arbitrary template lookup."""

    E1_CREDENTIALS = "E1_credentials"
    E2_INVITE = "E2_invite"
    E3_CANDIDATE_REPORT = "E3_candidate_report"
    E4_SHORTLIST = "E4_shortlist"
    E5_COMPLETION = "E5_completion"


class E1Purpose(StrEnum):
    INITIAL_CREDENTIAL = "initial_credential"
    PASSWORD_RESET_TOKEN = "password_reset_token"  # pragma: allowlist secret -- enum label


def safe_text(value: str) -> str:
    """Normalize user-influenced text and remove invisible direction controls."""
    normalized = unicodedata.normalize("NFKC", value)
    safe = "".join(
        character
        for character in normalized
        if character not in _FORMAT_CONTROLS
        and not unicodedata.category(character).startswith("C")
    ).strip()
    if not safe:
        raise ValueError("email display text is empty after normalization")
    return safe


def render_e1(
    *,
    email_send_id: UUID,
    recipient: str,
    recipient_name: str,
    sender: str,
    purpose: E1Purpose,
    secret: str,
    public_app_url: str,
) -> OutboundEmail:
    """Render credentials or a reset link without an unsafe HTML sink."""
    recipient = validate_address(recipient)
    sender = validate_address(sender)
    name = safe_text(recipient_name)
    base = public_app_url.rstrip("/")
    sign_in_url = f"{base}/sign-in"
    if purpose is E1Purpose.INITIAL_CREDENTIAL:
        subject = "Your BlueLab account"
        text_body = (
            f"Hello {name},\n\n"
            "Your BlueLab account is ready.\n"
            f"Sign in: {sign_in_url}\n"
            f"Initial credential: {secret}\n\n"
            "You will set a new password when you first sign in.\n"
        )
        html_body = (
            f"<p>Hello {html.escape(name)},</p>"
            "<p>Your BlueLab account is ready.</p>"
            f'<p><a href="{html.escape(sign_in_url, quote=True)}">Sign in to BlueLab</a></p>'
            f"<p>Initial credential: <code>{html.escape(secret)}</code></p>"
            "<p>You will set a new password when you first sign in.</p>"
        )
    elif purpose is E1Purpose.PASSWORD_RESET_TOKEN:
        subject = "Reset your BlueLab password"
        reset_url = f"{base}/reset-password#token={quote(secret, safe='')}"
        text_body = (
            f"Hello {name},\n\n"
            "Use this time-limited, single-use link to reset your BlueLab password:\n"
            f"{reset_url}\n\n"
            "If you did not request this, you can ignore this message.\n"
        )
        html_body = (
            f"<p>Hello {html.escape(name)},</p>"
            "<p>Use this time-limited, single-use link to reset your BlueLab password:</p>"
            f'<p><a href="{html.escape(reset_url, quote=True)}">Reset password</a></p>'
            "<p>If you did not request this, you can ignore this message.</p>"
        )
    else:  # pragma: no cover - StrEnum makes the closed branch unconstructible
        raise ValueError("unsupported E-1 purpose")

    return OutboundEmail(
        recipient=recipient,
        sender=sender,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        message_id=f"<{email_send_id}@notifications.bluelab>",
    )
