"""Runtime-safe call bundle defined by api/02 §1; unknown fields fail closed."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CallType = Literal["discovery", "post_proposal", "renewal", "upsell"]
LeadType = Literal["inbound_quote", "referral", "cold_outreach"]


class Participant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["rep", "candidate", "author"]
    identity: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


class Scenario(BaseModel):
    """Participant-safe frozen scenario; evaluator truth has no representable field."""

    model_config = ConfigDict(extra="forbid")

    buyer_name: str = Field(min_length=1)
    buyer_role: str = Field(min_length=1)
    buyer_company: str = Field(min_length=1)
    buyer_meta: dict[str, str] = Field(default_factory=dict)
    situation_context: str = Field(min_length=1)
    participant_product_summary: str = Field(min_length=1)


class PersonaSections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    who_you_are: str = Field(min_length=1)
    your_world: str = Field(min_length=1)
    where_you_are_right_now: str = Field(min_length=1)
    call_context: str = ""


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stt_provider: Literal["speechmatics"] = "speechmatics"
    stt_language: str = "ar_en"
    stt_operating_point: Literal["standard", "enhanced"] = "enhanced"
    llm_provider: Literal["anthropic"] = "anthropic"
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_temperature: float = Field(default=0.8, ge=0, le=1)
    llm_max_tokens: int = Field(default=1024, ge=1, le=4096)
    llm_prompt_caching: bool = True
    tts_provider: Literal["elevenlabs", "azure"] = "elevenlabs"
    tts_model: str = "elevenlabs/eleven_flash_v2_5"
    turn_detection: Literal["vad", "stt", "multilingual"] = "vad"
    allow_interruptions: bool = True
    min_endpointing_delay: float = Field(default=0.4, ge=0)
    max_endpointing_delay: float = Field(default=6.0, ge=0)


class RuntimeBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = 1
    call_id: str = Field(min_length=1)
    mode: Literal["attempt", "test"]
    attempt_id: str | None = None
    drill_id: str = Field(min_length=1)
    org_id: str = Field(min_length=1)
    team_id: str = Field(min_length=1)
    participant: Participant
    scenario: Scenario
    persona: PersonaSections
    language: str = Field(min_length=2, max_length=16)
    call_type: CallType
    lead_type: LeadType | None = None
    voice_identity: str = Field(min_length=1)
    runtime_config: RuntimeConfig = Field(default_factory=RuntimeConfig)
    max_call_seconds: int = Field(default=900, ge=1, le=900)
    reconnect_grace_seconds: int = Field(default=30, ge=0, le=60)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def attempt_matches_mode(self) -> RuntimeBundle:
        if self.mode == "attempt" and not self.attempt_id:
            raise ValueError("attempt_id is required when mode=attempt")
        if self.mode == "test" and self.attempt_id is not None:
            raise ValueError("attempt_id must be absent when mode=test")
        if self.call_type == "discovery" and self.lead_type is None:
            raise ValueError("lead_type is required when call_type=discovery")
        if self.call_type != "discovery" and self.lead_type is not None:
            raise ValueError("lead_type must be absent unless call_type=discovery")
        return self

    @property
    def is_arabic_or_mixed(self) -> bool:
        language = self.language.lower()
        return language.startswith("ar") or language in {"mixed", "ar-en", "ar_en", "multi"}
