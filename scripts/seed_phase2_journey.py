#!/usr/bin/env python3
"""Create one synthetic operations identity for the connected Phase 2 journey.

The command is local-only. It prints a fresh password and TOTP seed once so the
browser journey can authenticate without a test-only HTTP endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import secrets
from typing import Any

from sqlalchemy import create_engine, text

from bluelab.adapters.secrets import LocalAeadCipher, OpsTotpContext
from bluelab.platform.config import Environment, Settings, get_settings
from bluelab.platform.ids import new_id
from bluelab.platform.security.passwords import hash_password


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email-domain",
        default="example.com",
        help="Reserved synthetic domain used for the generated operations account.",
    )
    return parser.parse_args()


async def seed(settings: Settings, *, email_domain: str) -> dict[str, str]:
    if settings.environment is not Environment.LOCAL:
        raise RuntimeError("the connected-journey seed is available only in BLUELAB_ENV=local")
    if settings.migration_database_url is None:
        raise RuntimeError("MIGRATION_DATABASE_URL is required")
    if settings.ops_totp_local_key is None:
        raise RuntimeError("OPS_TOTP_LOCAL_KEY is required")

    ops_account_id = new_id()
    suffix = str(ops_account_id).replace("-", "")[-12:]
    email = f"phase2-operator-{suffix}@{email_domain.lower().rstrip('.')}"
    password = secrets.token_urlsafe(24)
    totp_seed = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    cipher = LocalAeadCipher.from_base64(settings.ops_totp_local_key.get_secret_value())
    ciphertext = await cipher.seal(
        totp_seed,
        context=OpsTotpContext(ops_account_id=ops_account_id),
    )

    engine = create_engine(settings.migration_database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            available_kinds = set(
                connection.execute(
                    text(
                        "select distinct on (kind) kind from legal_document_version "
                        "where effective_at <= now() order by kind, effective_at desc, id desc"
                    )
                ).scalars()
            )
            required_kinds = {
                "recording_consent_notice",
                "terms_of_use",
                "privacy_notice",
            }
            missing = sorted(required_kinds - available_kinds)
            if missing:
                raise RuntimeError(
                    "the migrated legal-document catalog is incomplete: " + ", ".join(missing)
                )
            connection.execute(
                text(
                    "insert into ops_account "
                    "(id, email, display_name, password_hash, totp_secret_ciphertext, status) "
                    "values (:id, :email, :display_name, :password_hash, :totp, 'active')"
                ),
                {
                    "id": ops_account_id,
                    "email": email,
                    "display_name": "Phase 2 Journey Operator",
                    "password_hash": hash_password(password),
                    "totp": ciphertext,
                },
            )
    finally:
        engine.dispose()

    return {
        "ops_email": email,
        "ops_password": password,
        "ops_totp_seed": totp_seed,
    }


def main() -> int:
    args = parse_args()
    result: dict[str, Any] = asyncio.run(seed(get_settings(), email_domain=args.email_domain))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
