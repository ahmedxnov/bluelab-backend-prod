"""LiveKit placement and least-privilege participant grants."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Literal

from livekit import api
from livekit.protocol.agent_dispatch import RoomAgentDispatch
from livekit.protocol.room import RoomConfiguration
from pydantic import BaseModel, ConfigDict, model_validator

from bluelab.calls.registry import CallSession

TOKEN_TTL_SECONDS = 60
AGENT_NAME = "bluelab-buyer"


class DispatchParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["rep", "candidate", "author"]
    identity: str
    display_name: str


class DispatchPayload(BaseModel):
    """The closed, participant-visible explicit-dispatch contract."""

    model_config = ConfigDict(extra="forbid")
    contract_version: Literal[1] = 1
    call_id: str
    mode: Literal["attempt", "test"]
    attempt_id: str | None = None
    drill_id: str
    org_id: str
    team_id: str
    participant: DispatchParticipant
    max_call_seconds: Literal[900] = 900
    reconnect_grace_seconds: Literal[30] = 30

    @model_validator(mode="after")
    def attempt_matches_mode(self) -> DispatchPayload:
        if (self.mode == "attempt") != (self.attempt_id is not None):
            raise ValueError("attempt_id presence must match dispatch mode")
        return self


def mint_participant_token(
    *, api_key: str, api_secret: str, call: CallSession
) -> str:
    """Mint a 60-second token bound to one room and microphone publication."""
    room = f"call_{call.call_id}"
    metadata = DispatchPayload(
        call_id=str(call.call_id),
        mode=call.mode,
        attempt_id=str(call.attempt_id) if call.attempt_id else None,
        drill_id=str(call.drill_id),
        org_id=str(call.org_id),
        team_id=str(call.team_id),
        participant=DispatchParticipant(
            kind=call.participant_kind,
            identity=call.participant_identity,
            display_name=call.participant_display_name,
        ),
    ).model_dump(mode="json", exclude_none=True)
    room_config = RoomConfiguration(
        agents=[
            RoomAgentDispatch(
                agent_name=AGENT_NAME,
                metadata=json.dumps(metadata, separators=(",", ":")),
            )
        ]
    )
    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(call.participant_identity)
        .with_ttl(timedelta(seconds=TOKEN_TTL_SECONDS))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
                can_publish_sources=["microphone"],
            )
        )
        .with_room_config(room_config)
        .to_jwt()
    )
