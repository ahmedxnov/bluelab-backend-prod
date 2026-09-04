"""initial schema — 38 tables, data/01 §1-§9

Revision ID: 0001_initial_schema
Revises:
Created: 2026-07-30

The whole schema in one migration, because at this point there is no released
version N to stay compatible with — the compatibility window (data/04 §2) starts
binding from the *second* migration onwards.

**Reviewed DDL, not autogenerate output.** Rendered from the SQLAlchemy metadata
so the text a reviewer reads is the text PostgreSQL receives. Table order is
`metadata.sorted_tables`, which resolves the foreign-key dependencies;
`account.team_id` self-references and needs no special handling.

**No `downgrade()`** — the lineage is forward-only (data/04 §1).

**Deliberately absent from this file**, and from the lineage generally
(data/04 §3):

    RLS policies          sql/policies/generated/  — regenerated + drift-checked
    freeze guards         sql/triggers/
    fn_score_band         sql/functions/
    views V-1…V-11        sql/views/
    roles and grants      sql/roles/bootstrap.sql  — cluster-level, not in pg_dump

Those are re-applied idempotently at every release. Putting them here would make
them un-regenerable and the drift check meaningless.

**The extension allowlist is empty** (data/00 §2, data/04 §8). No citext — the
case-insensitive unique keys are functional indexes on `lower(...)`. No pgcrypto
— hashing is application-side. Nothing host-specific anywhere, which is what
keeps a host migration a connection-string change.
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── authoring_option — drills (P0 reference) ───────────────
    op.execute(
        """
CREATE TABLE authoring_option (
	id UUID NOT NULL, 
	kind TEXT NOT NULL, 
	label TEXT NOT NULL, 
	active BOOLEAN DEFAULT true NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_authoring_option PRIMARY KEY (id), 
	CONSTRAINT ck_authoring_option_kind_valid CHECK (kind in ('challenge', 'hidden_motive'))
)
        """
    )

    # ── badge — training (P0 reference) ────────────────────────
    op.execute(
        """
CREATE TABLE badge (
	code TEXT NOT NULL, 
	name TEXT NOT NULL, 
	rule_text TEXT NOT NULL, 
	active BOOLEAN DEFAULT true NOT NULL, 
	CONSTRAINT pk_badge PRIMARY KEY (code)
)
        """
    )

    # ── legal_document_version — identity (P0 reference) ───────
    op.execute(
        """
CREATE TABLE legal_document_version (
	id UUID NOT NULL, 
	kind TEXT NOT NULL, 
	version TEXT NOT NULL, 
	effective_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_legal_document_version PRIMARY KEY (id), 
	CONSTRAINT ck_legal_document_version_kind_valid CHECK (kind in ('recording_consent_notice', 'terms_of_use', 'privacy_notice')), 
	CONSTRAINT uq_legal_document_version_kind_version UNIQUE (kind, version)
)
        """
    )

    # ── ops_account — operations ───────────────────────────────
    op.execute(
        """
CREATE TABLE ops_account (
	id UUID NOT NULL, 
	email TEXT NOT NULL, 
	display_name TEXT NOT NULL, 
	password_hash TEXT NOT NULL, 
	status TEXT DEFAULT 'active' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_ops_account PRIMARY KEY (id), 
	CONSTRAINT ck_ops_account_status_valid CHECK (status in ('active', 'deactivated'))
)
        """
    )
    op.execute(
        """
CREATE UNIQUE INDEX uq_ops_account_email ON ops_account (lower(email))
        """
    )

    # ── org — identity ─────────────────────────────────────────
    op.execute(
        """
CREATE TABLE org (
	id UUID NOT NULL, 
	name TEXT NOT NULL, 
	timezone TEXT DEFAULT 'Africa/Cairo' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_org PRIMARY KEY (id)
)
        """
    )

    # ── account — identity ─────────────────────────────────────
    op.execute(
        """
CREATE TABLE account (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	email TEXT NOT NULL, 
	display_name TEXT NOT NULL, 
	role TEXT NOT NULL, 
	password_hash TEXT NOT NULL, 
	credential_state TEXT DEFAULT 'initial' NOT NULL, 
	status TEXT DEFAULT 'active' NOT NULL, 
	deactivated_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_account PRIMARY KEY (id), 
	CONSTRAINT ck_account_role_valid CHECK (role in ('manager', 'rep')), 
	CONSTRAINT ck_account_credential_state_valid CHECK (credential_state in ('initial', 'set')), 
	CONSTRAINT ck_account_status_valid CHECK (status in ('active', 'deactivated')), 
	CONSTRAINT ck_account_team_is_owning_manager CHECK ((role = 'manager' and team_id = id) or (role = 'rep' and team_id <> id)), 
	CONSTRAINT uq_account_id_org_id UNIQUE (id, org_id), 
	CONSTRAINT uq_account_id_org_id_team_id UNIQUE (id, org_id, team_id), 
	CONSTRAINT fk_account_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_account_team_id FOREIGN KEY(team_id) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_account_org_id ON account (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_account_team_id ON account (team_id)
        """
    )
    op.execute(
        """
CREATE UNIQUE INDEX uq_account_email ON account (lower(email))
        """
    )

    # ── erasure_request — operations ───────────────────────────
    op.execute(
        """
CREATE TABLE erasure_request (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	subject_kind TEXT NOT NULL, 
	subject_id UUID NOT NULL, 
	status TEXT DEFAULT 'pending' NOT NULL, 
	requested_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	executed_at TIMESTAMP WITH TIME ZONE, 
	executed_by UUID, 
	evidence JSONB DEFAULT '{}' NOT NULL, 
	CONSTRAINT pk_erasure_request PRIMARY KEY (id), 
	CONSTRAINT ck_erasure_request_subject_kind_valid CHECK (subject_kind in ('account', 'candidate')), 
	CONSTRAINT ck_erasure_request_status_valid CHECK (status in ('pending', 'executed', 'failed')), 
	CONSTRAINT fk_erasure_request_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_erasure_request_executed_by FOREIGN KEY(executed_by) REFERENCES ops_account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_erasure_request_org_id ON erasure_request (org_id)
        """
    )

    # ── export_request — operations ────────────────────────────
    op.execute(
        """
CREATE TABLE export_request (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	subject_kind TEXT NOT NULL, 
	subject_id UUID NOT NULL, 
	status TEXT DEFAULT 'pending' NOT NULL, 
	bundle_object_key TEXT, 
	requested_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	ready_at TIMESTAMP WITH TIME ZONE, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_export_request PRIMARY KEY (id), 
	CONSTRAINT ck_export_request_subject_kind_valid CHECK (subject_kind in ('account', 'candidate')), 
	CONSTRAINT ck_export_request_status_valid CHECK (status in ('pending', 'ready', 'delivered', 'failed')), 
	CONSTRAINT fk_export_request_org_id FOREIGN KEY(org_id) REFERENCES org (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_export_request_org_id ON export_request (org_id)
        """
    )

    # ── ops_audit — operations ─────────────────────────────────
    op.execute(
        """
CREATE TABLE ops_audit (
	id UUID NOT NULL, 
	ops_account_id UUID NOT NULL, 
	verb TEXT NOT NULL, 
	target_org_id UUID, 
	target_ref JSONB DEFAULT '{}' NOT NULL, 
	reason TEXT NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_ops_audit PRIMARY KEY (id), 
	CONSTRAINT ck_ops_audit_verb_valid CHECK (verb in ('provision_org', 'provision_account', 'deactivate_account', 'change_team_mapping', 'transfer_position', 'resolve_fault', 'execute_erasure', 'execute_export')), 
	CONSTRAINT fk_ops_audit_ops_account_id FOREIGN KEY(ops_account_id) REFERENCES ops_account (id), 
	CONSTRAINT fk_ops_audit_target_org_id FOREIGN KEY(target_org_id) REFERENCES org (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_audit_org_time ON ops_audit (target_org_id, occurred_at desc)
        """
    )

    # ── badge_award — training ─────────────────────────────────
    op.execute(
        """
CREATE TABLE badge_award (
	badge_code TEXT NOT NULL, 
	account_id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	earned_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_badge_award PRIMARY KEY (badge_code, account_id), 
	CONSTRAINT fk_badge_award_badge_code FOREIGN KEY(badge_code) REFERENCES badge (code), 
	CONSTRAINT fk_badge_award_account_id FOREIGN KEY(account_id) REFERENCES account (id), 
	CONSTRAINT fk_badge_award_org_id FOREIGN KEY(org_id) REFERENCES org (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_badge_award_org_id ON badge_award (org_id)
        """
    )

    # ── coach_feedback_item — training ─────────────────────────
    op.execute(
        """
CREATE TABLE coach_feedback_item (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	rep_account_id UUID NOT NULL, 
	body TEXT NOT NULL, 
	read_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_coach_feedback_item PRIMARY KEY (id), 
	CONSTRAINT fk_coach_feedback_item_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_coach_feedback_item_rep_account_id FOREIGN KEY(rep_account_id) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_feedback_rep ON coach_feedback_item (rep_account_id, created_at desc)
        """
    )
    op.execute(
        """
CREATE INDEX idx_feedback_unread ON coach_feedback_item (rep_account_id) WHERE read_at is null
        """
    )
    op.execute(
        """
CREATE INDEX ix_coach_feedback_item_org_id ON coach_feedback_item (org_id)
        """
    )

    # ── drill — drills ─────────────────────────────────────────
    op.execute(
        """
CREATE TABLE drill (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	author_account_id UUID NOT NULL, 
	self_authored BOOLEAN NOT NULL, 
	status TEXT DEFAULT 'draft' NOT NULL, 
	call_type TEXT NOT NULL, 
	lead_type TEXT, 
	language TEXT DEFAULT 'ar-EG' NOT NULL, 
	label TEXT, 
	scenario JSONB, 
	answer_key JSONB, 
	content_hash TEXT, 
	published_at TIMESTAMP WITH TIME ZONE, 
	archived_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_drill PRIMARY KEY (id), 
	CONSTRAINT ck_drill_status_valid CHECK (status in ('draft', 'published', 'archived')), 
	CONSTRAINT ck_drill_call_type_valid CHECK (call_type in ('discovery', 'post_proposal', 'renewal', 'upsell')), 
	CONSTRAINT ck_drill_lead_type_valid CHECK (lead_type in ('inbound_quote', 'referral', 'cold_outreach')), 
	CONSTRAINT ck_drill_language_v1 CHECK (language = 'ar-EG'), 
	CONSTRAINT ck_drill_lead_type_on_discovery CHECK ((call_type = 'discovery') = (lead_type is not null)), 
	CONSTRAINT ck_drill_published_is_complete CHECK ((status not in ('published','archived')) or (scenario is not null and answer_key is not null and label is not null and published_at is not null)), 
	CONSTRAINT uq_drill_id_org_id UNIQUE (id, org_id), 
	CONSTRAINT uq_drill_id_org_id_self_authored UNIQUE (id, org_id, self_authored), 
	CONSTRAINT uq_drill_id_org_id_team_id UNIQUE (id, org_id, team_id), 
	CONSTRAINT fk_drill_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_drill_team_id FOREIGN KEY(team_id) REFERENCES account (id), 
	CONSTRAINT fk_drill_author_account_id FOREIGN KEY(author_account_id) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_drill_author_self ON drill (author_account_id) WHERE self_authored
        """
    )
    op.execute(
        """
CREATE INDEX idx_drill_team_status ON drill (team_id, status)
        """
    )
    op.execute(
        """
CREATE INDEX ix_drill_org_id ON drill (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_drill_team_id ON drill (team_id)
        """
    )

    # ── hr_contact — hiring ────────────────────────────────────
    op.execute(
        """
CREATE TABLE hr_contact (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	owner_account_id UUID NOT NULL, 
	email TEXT NOT NULL, 
	label TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_hr_contact PRIMARY KEY (id), 
	CONSTRAINT fk_hr_contact_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_hr_contact_owner_account_id FOREIGN KEY(owner_account_id) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_hr_contact_org_id ON hr_contact (org_id)
        """
    )
    op.execute(
        """
CREATE UNIQUE INDEX uq_hr_contact ON hr_contact (owner_account_id, lower(email))
        """
    )

    # ── password_reset_token — identity ────────────────────────
    op.execute(
        """
CREATE TABLE password_reset_token (
	id UUID NOT NULL, 
	account_id UUID NOT NULL, 
	token_hash TEXT NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	used_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_password_reset_token PRIMARY KEY (id), 
	CONSTRAINT fk_password_reset_token_account_id FOREIGN KEY(account_id) REFERENCES account (id), 
	CONSTRAINT uq_password_reset_token_token_hash UNIQUE (token_hash)
)
        """
    )

    # ── position — hiring ──────────────────────────────────────
    op.execute(
        """
CREATE TABLE position (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	title TEXT NOT NULL, 
	openings SMALLINT NOT NULL, 
	notify_on_completion BOOLEAN DEFAULT false NOT NULL, 
	report_policy TEXT DEFAULT 'rejected_only' NOT NULL, 
	invite_expiry_days SMALLINT DEFAULT 7 NOT NULL, 
	invite_template TEXT, 
	status TEXT DEFAULT 'needs_authoring' NOT NULL, 
	assessment_frozen_at TIMESTAMP WITH TIME ZONE, 
	closed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_position PRIMARY KEY (id), 
	CONSTRAINT ck_position_report_policy_valid CHECK (report_policy in ('after_finish', 'rejected_only', 'withhold')), 
	CONSTRAINT ck_position_status_valid CHECK (status in ('needs_authoring', 'active', 'closed')), 
	CONSTRAINT ck_position_openings_positive CHECK (openings > 0), 
	CONSTRAINT ck_position_invite_expiry_positive CHECK (invite_expiry_days > 0), 
	CONSTRAINT uq_position_id_org_id_team_id UNIQUE (id, org_id, team_id), 
	CONSTRAINT fk_position_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_position_team_id FOREIGN KEY(team_id) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_position_org_id ON position (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_position_team_id ON position (team_id)
        """
    )

    # ── product_document — knowledge ───────────────────────────
    op.execute(
        """
CREATE TABLE product_document (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	title TEXT NOT NULL, 
	live_version INTEGER DEFAULT 0 NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_product_document PRIMARY KEY (id), 
	CONSTRAINT uq_product_document_id_org_id_team_id UNIQUE (id, org_id, team_id), 
	CONSTRAINT fk_product_document_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_product_document_team_id FOREIGN KEY(team_id) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_product_document_org_id ON product_document (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_product_document_team_id ON product_document (team_id)
        """
    )

    # ── assessment_stage — hiring ──────────────────────────────
    op.execute(
        """
CREATE TABLE assessment_stage (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	position_id UUID NOT NULL, 
	ord INTEGER NOT NULL, 
	drill_id UUID NOT NULL, 
	CONSTRAINT pk_assessment_stage PRIMARY KEY (id), 
	CONSTRAINT fk_assessment_stage_position_id_org_id_team_id FOREIGN KEY(position_id, org_id, team_id) REFERENCES position (id, org_id, team_id) ON DELETE CASCADE ON UPDATE CASCADE, 
	CONSTRAINT fk_assessment_stage_drill_id_org_id FOREIGN KEY(drill_id, org_id) REFERENCES drill (id, org_id), 
	CONSTRAINT uq_assessment_stage_position_id_ord UNIQUE (position_id, ord) DEFERRABLE INITIALLY DEFERRED
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_stage_drill ON assessment_stage (drill_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_assessment_stage_org_id ON assessment_stage (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_assessment_stage_team_id ON assessment_stage (team_id)
        """
    )

    # ── assignment — drills ────────────────────────────────────
    op.execute(
        """
CREATE TABLE assignment (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	drill_id UUID NOT NULL, 
	due_date DATE NOT NULL, 
	attempts_allowed SMALLINT NOT NULL, 
	created_by UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_assignment PRIMARY KEY (id), 
	CONSTRAINT ck_assignment_attempts_allowed_positive CHECK (attempts_allowed > 0), 
	CONSTRAINT fk_assignment_drill_id_org_id_team_id FOREIGN KEY(drill_id, org_id, team_id) REFERENCES drill (id, org_id, team_id) ON DELETE CASCADE, 
	CONSTRAINT uq_assignment_id_org_id_team_id UNIQUE (id, org_id, team_id), 
	CONSTRAINT uq_assignment_drill_id UNIQUE (drill_id), 
	CONSTRAINT fk_assignment_created_by FOREIGN KEY(created_by) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_assignment_org_id ON assignment (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_assignment_team_id ON assignment (team_id)
        """
    )

    # ── candidate — hiring ─────────────────────────────────────
    op.execute(
        """
CREATE TABLE candidate (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	position_id UUID NOT NULL, 
	name TEXT NOT NULL, 
	email TEXT NOT NULL, 
	phone TEXT, 
	linkedin TEXT, 
	source TEXT, 
	internal_note TEXT, 
	preflight JSONB, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	decision TEXT DEFAULT 'pending' NOT NULL, 
	decided_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_candidate PRIMARY KEY (id), 
	CONSTRAINT ck_candidate_decision_valid CHECK (decision in ('pending', 'approved', 'rejected')), 
	CONSTRAINT fk_candidate_position_id_org_id_team_id FOREIGN KEY(position_id, org_id, team_id) REFERENCES position (id, org_id, team_id) ON UPDATE CASCADE, 
	CONSTRAINT uq_candidate_id_org_id_team_id UNIQUE (id, org_id, team_id), 
	CONSTRAINT fk_candidate_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_candidate_team_id FOREIGN KEY(team_id) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_candidate_position ON candidate (position_id, decision)
        """
    )
    op.execute(
        """
CREATE INDEX ix_candidate_org_id ON candidate (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_candidate_team_id ON candidate (team_id)
        """
    )

    # ── document_upload — knowledge ────────────────────────────
    op.execute(
        """
CREATE TABLE document_upload (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	document_id UUID NOT NULL, 
	object_key TEXT NOT NULL, 
	filename TEXT NOT NULL, 
	byte_size INTEGER NOT NULL, 
	status TEXT DEFAULT 'received' NOT NULL, 
	failure_reason TEXT, 
	created_by UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_document_upload PRIMARY KEY (id), 
	CONSTRAINT ck_document_upload_status_valid CHECK (status in ('received', 'extracting', 'extracted', 'failed')), 
	CONSTRAINT ck_document_upload_byte_size_range CHECK (byte_size between 1 and 20971520), 
	CONSTRAINT fk_document_upload_document_id_org_id_team_id FOREIGN KEY(document_id, org_id, team_id) REFERENCES product_document (id, org_id, team_id) ON DELETE CASCADE, 
	CONSTRAINT fk_document_upload_created_by FOREIGN KEY(created_by) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_upload_document ON document_upload (document_id, created_at desc)
        """
    )
    op.execute(
        """
CREATE INDEX ix_document_upload_org_id ON document_upload (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_document_upload_team_id ON document_upload (team_id)
        """
    )

    # ── drill_concealed — drills ───────────────────────────────
    op.execute(
        """
CREATE TABLE drill_concealed (
	drill_id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	challenges JSONB DEFAULT '[]' NOT NULL, 
	hidden_motives JSONB DEFAULT '[]' NOT NULL, 
	CONSTRAINT pk_drill_concealed PRIMARY KEY (drill_id), 
	CONSTRAINT fk_drill_concealed_drill_id_org_id_team_id FOREIGN KEY(drill_id, org_id, team_id) REFERENCES drill (id, org_id, team_id) ON DELETE CASCADE
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_drill_concealed_org_id ON drill_concealed (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_drill_concealed_team_id ON drill_concealed (team_id)
        """
    )

    # ── fact_set — knowledge ───────────────────────────────────
    op.execute(
        """
CREATE TABLE fact_set (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	document_id UUID NOT NULL, 
	kind TEXT NOT NULL, 
	source TEXT NOT NULL, 
	based_on_version INTEGER, 
	created_by UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_fact_set PRIMARY KEY (id), 
	CONSTRAINT ck_fact_set_kind_valid CHECK (kind in ('live', 'draft')), 
	CONSTRAINT ck_fact_set_source_valid CHECK (source in ('upload', 'manual')), 
	CONSTRAINT ck_fact_set_draft_carries_base_version CHECK ((kind = 'draft') = (based_on_version is not null)), 
	CONSTRAINT fk_fact_set_document_id_org_id_team_id FOREIGN KEY(document_id, org_id, team_id) REFERENCES product_document (id, org_id, team_id) ON DELETE CASCADE, 
	CONSTRAINT fk_fact_set_created_by FOREIGN KEY(created_by) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_fact_set_org_id ON fact_set (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_fact_set_team_id ON fact_set (team_id)
        """
    )
    op.execute(
        """
CREATE UNIQUE INDEX uq_fact_set_draft ON fact_set (document_id) WHERE kind = 'draft'
        """
    )
    op.execute(
        """
CREATE UNIQUE INDEX uq_fact_set_live ON fact_set (document_id) WHERE kind = 'live'
        """
    )

    # ── rubric_dimension — drills ──────────────────────────────
    op.execute(
        """
CREATE TABLE rubric_dimension (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	drill_id UUID NOT NULL, 
	ord INTEGER NOT NULL, 
	name TEXT NOT NULL, 
	weight SMALLINT NOT NULL, 
	rationale TEXT NOT NULL, 
	CONSTRAINT pk_rubric_dimension PRIMARY KEY (id), 
	CONSTRAINT ck_rubric_dimension_weight_range CHECK (weight >= 0 and weight <= 100), 
	CONSTRAINT fk_rubric_dimension_drill_id_org_id_team_id FOREIGN KEY(drill_id, org_id, team_id) REFERENCES drill (id, org_id, team_id) ON DELETE CASCADE, 
	CONSTRAINT uq_rubric_dimension_drill_id_ord UNIQUE (drill_id, ord) DEFERRABLE INITIALLY DEFERRED
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_rubric_dimension_org_id ON rubric_dimension (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_rubric_dimension_team_id ON rubric_dimension (team_id)
        """
    )

    # ── shortlist — hiring ─────────────────────────────────────
    op.execute(
        """
CREATE TABLE shortlist (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	position_id UUID NOT NULL, 
	sent_by UUID NOT NULL, 
	recipients JSONB NOT NULL, 
	email_body TEXT NOT NULL, 
	sent_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_shortlist PRIMARY KEY (id), 
	CONSTRAINT fk_shortlist_position_id_org_id_team_id FOREIGN KEY(position_id, org_id, team_id) REFERENCES position (id, org_id, team_id) ON UPDATE CASCADE, 
	CONSTRAINT uq_shortlist_id_org_id_team_id UNIQUE (id, org_id, team_id), 
	CONSTRAINT fk_shortlist_sent_by FOREIGN KEY(sent_by) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_shortlist_position ON shortlist (position_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_shortlist_org_id ON shortlist (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_shortlist_team_id ON shortlist (team_id)
        """
    )

    # ── assignment_recipient — drills ──────────────────────────
    op.execute(
        """
CREATE TABLE assignment_recipient (
	assignment_id UUID NOT NULL, 
	rep_account_id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	attempts_used SMALLINT DEFAULT 0 NOT NULL, 
	granted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_assignment_recipient PRIMARY KEY (assignment_id, rep_account_id), 
	CONSTRAINT ck_assignment_recipient_attempts_used_non_negative CHECK (attempts_used >= 0), 
	CONSTRAINT fk_assignment_recipient_assignment_id_org_id_team_id FOREIGN KEY(assignment_id, org_id, team_id) REFERENCES assignment (id, org_id, team_id) ON DELETE CASCADE, 
	CONSTRAINT fk_assignment_recipient_rep_account_id FOREIGN KEY(rep_account_id) REFERENCES account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_ar_rep ON assignment_recipient (rep_account_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_assignment_recipient_org_id ON assignment_recipient (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_assignment_recipient_team_id ON assignment_recipient (team_id)
        """
    )

    # ── attempt — review ───────────────────────────────────────
    op.execute(
        """
CREATE TABLE attempt (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	drill_id UUID NOT NULL, 
	rep_account_id UUID, 
	candidate_id UUID, 
	assessment_stage_id UUID, 
	self_authored BOOLEAN DEFAULT false NOT NULL, 
	status TEXT DEFAULT 'in_progress' NOT NULL, 
	restart BOOLEAN DEFAULT false NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	ended_at TIMESTAMP WITH TIME ZONE, 
	duration_seconds INTEGER, 
	recording_object_key TEXT, 
	recording_status TEXT DEFAULT 'none' NOT NULL, 
	CONSTRAINT pk_attempt PRIMARY KEY (id), 
	CONSTRAINT ck_attempt_status_valid CHECK (status in ('in_progress', 'completed', 'grading_pending', 'graded', 'interrupted')), 
	CONSTRAINT ck_attempt_recording_status_valid CHECK (recording_status in ('none', 'pending', 'available', 'unavailable', 'erased')), 
	CONSTRAINT ck_attempt_exactly_one_participant CHECK (num_nonnulls(rep_account_id, candidate_id) = 1), 
	CONSTRAINT ck_attempt_candidate_has_stage CHECK ((candidate_id is null) = (assessment_stage_id is null)), 
	CONSTRAINT ck_attempt_ends_after_start CHECK (ended_at is null or ended_at >= started_at), 
	CONSTRAINT ck_attempt_duration_non_negative CHECK (duration_seconds is null or duration_seconds >= 0), 
	CONSTRAINT fk_attempt_drill_id_org_id_self_authored FOREIGN KEY(drill_id, org_id, self_authored) REFERENCES drill (id, org_id, self_authored), 
	CONSTRAINT fk_attempt_rep_account_id_org_id_team_id FOREIGN KEY(rep_account_id, org_id, team_id) REFERENCES account (id, org_id, team_id) ON UPDATE CASCADE, 
	CONSTRAINT fk_attempt_candidate_id_org_id_team_id FOREIGN KEY(candidate_id, org_id, team_id) REFERENCES candidate (id, org_id, team_id) ON UPDATE CASCADE, 
	CONSTRAINT uq_attempt_id_org_id_team_id UNIQUE (id, org_id, team_id), 
	CONSTRAINT fk_attempt_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_attempt_assessment_stage_id FOREIGN KEY(assessment_stage_id) REFERENCES assessment_stage (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_attempt_candidate ON attempt (candidate_id, assessment_stage_id) WHERE candidate_id is not null
        """
    )
    op.execute(
        """
CREATE INDEX idx_attempt_drill_rep ON attempt (drill_id, rep_account_id)
        """
    )
    op.execute(
        """
CREATE INDEX idx_attempt_rep_time ON attempt (rep_account_id, started_at desc) WHERE rep_account_id is not null
        """
    )
    op.execute(
        """
CREATE INDEX idx_attempt_team_time ON attempt (team_id, started_at desc)
        """
    )
    op.execute(
        """
CREATE INDEX ix_attempt_org_id ON attempt (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_attempt_team_id ON attempt (team_id)
        """
    )
    op.execute(
        """
CREATE UNIQUE INDEX uq_attempt_stage_completed ON attempt (candidate_id, assessment_stage_id) WHERE candidate_id is not null and status in ('completed','grading_pending','graded')
        """
    )

    # ── candidate_report — hiring ──────────────────────────────
    op.execute(
        """
CREATE TABLE candidate_report (
	candidate_id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	takeaway TEXT, 
	generated_at TIMESTAMP WITH TIME ZONE, 
	pdf_object_key TEXT, 
	pdf_status TEXT DEFAULT 'none' NOT NULL, 
	pdf_rendered_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_candidate_report PRIMARY KEY (candidate_id), 
	CONSTRAINT ck_candidate_report_pdf_status_valid CHECK (pdf_status in ('none', 'pending', 'available', 'failed', 'erased')), 
	CONSTRAINT fk_candidate_report_candidate_id_org_id_team_id FOREIGN KEY(candidate_id, org_id, team_id) REFERENCES candidate (id, org_id, team_id) ON DELETE CASCADE ON UPDATE CASCADE
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_candidate_report_org_id ON candidate_report (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_candidate_report_team_id ON candidate_report (team_id)
        """
    )

    # ── candidate_token — identity ─────────────────────────────
    op.execute(
        """
CREATE TABLE candidate_token (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	candidate_id UUID NOT NULL, 
	token_hash TEXT NOT NULL, 
	issued_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_candidate_token PRIMARY KEY (id), 
	CONSTRAINT fk_candidate_token_candidate_id_org_id_team_id FOREIGN KEY(candidate_id, org_id, team_id) REFERENCES candidate (id, org_id, team_id) ON UPDATE CASCADE, 
	CONSTRAINT uq_candidate_token_token_hash UNIQUE (token_hash)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_token_candidate ON candidate_token (candidate_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_candidate_token_org_id ON candidate_token (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_candidate_token_team_id ON candidate_token (team_id)
        """
    )

    # ── consent_record — identity ──────────────────────────────
    op.execute(
        """
CREATE TABLE consent_record (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	account_id UUID, 
	candidate_id UUID, 
	notice_version TEXT NOT NULL, 
	consented_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_consent_record PRIMARY KEY (id), 
	CONSTRAINT ck_consent_record_exactly_one_subject CHECK (num_nonnulls(account_id, candidate_id) = 1), 
	CONSTRAINT uq_consent_record_account_id_notice_version UNIQUE (account_id, notice_version), 
	CONSTRAINT uq_consent_record_candidate_id_notice_version UNIQUE (candidate_id, notice_version), 
	CONSTRAINT fk_consent_record_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_consent_record_account_id FOREIGN KEY(account_id) REFERENCES account (id), 
	CONSTRAINT fk_consent_record_candidate_id FOREIGN KEY(candidate_id) REFERENCES candidate (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_consent_record_org_id ON consent_record (org_id)
        """
    )

    # ── product_fact — knowledge ───────────────────────────────
    op.execute(
        """
CREATE TABLE product_fact (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	fact_set_id UUID NOT NULL, 
	ord INTEGER NOT NULL, 
	label TEXT NOT NULL, 
	value TEXT NOT NULL, 
	note TEXT, 
	prior_fact_id UUID, 
	CONSTRAINT pk_product_fact PRIMARY KEY (id), 
	CONSTRAINT uq_product_fact_fact_set_id_ord UNIQUE (fact_set_id, ord), 
	CONSTRAINT fk_product_fact_fact_set_id FOREIGN KEY(fact_set_id) REFERENCES fact_set (id) ON DELETE CASCADE, 
	CONSTRAINT fk_product_fact_prior_fact_id FOREIGN KEY(prior_fact_id) REFERENCES product_fact (id) ON DELETE SET NULL
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_product_fact_org_id ON product_fact (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_product_fact_team_id ON product_fact (team_id)
        """
    )

    # ── shortlist_candidate — hiring ───────────────────────────
    op.execute(
        """
CREATE TABLE shortlist_candidate (
	shortlist_id UUID NOT NULL, 
	candidate_id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	CONSTRAINT pk_shortlist_candidate PRIMARY KEY (shortlist_id, candidate_id), 
	CONSTRAINT fk_shortlist_candidate_shortlist_id_org_id_team_id FOREIGN KEY(shortlist_id, org_id, team_id) REFERENCES shortlist (id, org_id, team_id) ON DELETE CASCADE ON UPDATE CASCADE, 
	CONSTRAINT fk_shortlist_candidate_candidate_id FOREIGN KEY(candidate_id) REFERENCES candidate (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_shortlist_member ON shortlist_candidate (candidate_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_shortlist_candidate_org_id ON shortlist_candidate (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_shortlist_candidate_team_id ON shortlist_candidate (team_id)
        """
    )

    # ── terms_acceptance — identity ────────────────────────────
    op.execute(
        """
CREATE TABLE terms_acceptance (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	account_id UUID, 
	candidate_id UUID, 
	terms_version TEXT NOT NULL, 
	privacy_version TEXT NOT NULL, 
	accepted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_terms_acceptance PRIMARY KEY (id), 
	CONSTRAINT ck_terms_acceptance_exactly_one_subject CHECK (num_nonnulls(account_id, candidate_id) = 1), 
	CONSTRAINT uq_terms_acceptance_account_id_terms_version_privacy_version UNIQUE (account_id, terms_version, privacy_version), 
	CONSTRAINT uq_terms_acceptance_candidate_id_terms_version_privacy_version UNIQUE (candidate_id, terms_version, privacy_version), 
	CONSTRAINT fk_terms_acceptance_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_terms_acceptance_account_id FOREIGN KEY(account_id) REFERENCES account (id), 
	CONSTRAINT fk_terms_acceptance_candidate_id FOREIGN KEY(candidate_id) REFERENCES candidate (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_terms_acceptance_org_id ON terms_acceptance (org_id)
        """
    )

    # ── email_send — notifications ─────────────────────────────
    op.execute(
        """
CREATE TABLE email_send (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	kind TEXT NOT NULL, 
	dedupe_key TEXT NOT NULL, 
	account_id UUID, 
	candidate_id UUID, 
	token_id UUID, 
	shortlist_id UUID, 
	status TEXT DEFAULT 'queued' NOT NULL, 
	provider_message_id TEXT, 
	sent_at TIMESTAMP WITH TIME ZONE, 
	last_event_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_email_send PRIMARY KEY (id), 
	CONSTRAINT ck_email_send_kind_valid CHECK (kind in ('E1_credentials', 'E2_invite', 'E3_candidate_report', 'E4_shortlist', 'E5_completion')), 
	CONSTRAINT ck_email_send_status_valid CHECK (status in ('queued', 'sent', 'delivered', 'bounced', 'delayed', 'failed')), 
	CONSTRAINT uq_email_send_kind_dedupe_key UNIQUE (kind, dedupe_key), 
	CONSTRAINT fk_email_send_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_email_send_account_id FOREIGN KEY(account_id) REFERENCES account (id), 
	CONSTRAINT fk_email_send_candidate_id FOREIGN KEY(candidate_id) REFERENCES candidate (id), 
	CONSTRAINT fk_email_send_token_id FOREIGN KEY(token_id) REFERENCES candidate_token (id) ON DELETE SET NULL, 
	CONSTRAINT fk_email_send_shortlist_id FOREIGN KEY(shortlist_id) REFERENCES shortlist (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_email_candidate ON email_send (candidate_id, created_at desc) WHERE candidate_id is not null
        """
    )
    op.execute(
        """
CREATE INDEX ix_email_send_org_id ON email_send (org_id)
        """
    )

    # ── ops_fault — operations ─────────────────────────────────
    op.execute(
        """
CREATE TABLE ops_fault (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	kind TEXT NOT NULL, 
	attempt_id UUID NOT NULL, 
	status TEXT DEFAULT 'open' NOT NULL, 
	detail JSONB DEFAULT '{}' NOT NULL, 
	opened_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	resolved_by UUID, 
	resolution_note TEXT, 
	CONSTRAINT pk_ops_fault PRIMARY KEY (id), 
	CONSTRAINT ck_ops_fault_kind_valid CHECK (kind in ('grading_failure', 'playback_asset')), 
	CONSTRAINT ck_ops_fault_status_valid CHECK (status in ('open', 'resolved')), 
	CONSTRAINT fk_ops_fault_org_id FOREIGN KEY(org_id) REFERENCES org (id), 
	CONSTRAINT fk_ops_fault_attempt_id FOREIGN KEY(attempt_id) REFERENCES attempt (id), 
	CONSTRAINT fk_ops_fault_resolved_by FOREIGN KEY(resolved_by) REFERENCES ops_account (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_fault_queue ON ops_fault (status, opened_at)
        """
    )
    op.execute(
        """
CREATE INDEX ix_ops_fault_org_id ON ops_fault (org_id)
        """
    )

    # ── scorecard — review ─────────────────────────────────────
    op.execute(
        """
CREATE TABLE scorecard (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	attempt_id UUID NOT NULL, 
	overall_score NUMERIC(3, 1) NOT NULL, 
	takeaway TEXT, 
	grading_meta JSONB DEFAULT '{}' NOT NULL, 
	graded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_scorecard PRIMARY KEY (id), 
	CONSTRAINT ck_scorecard_overall_score_range CHECK (overall_score >= 0 and overall_score <= 10), 
	CONSTRAINT fk_scorecard_attempt_id_org_id_team_id FOREIGN KEY(attempt_id, org_id, team_id) REFERENCES attempt (id, org_id, team_id) ON UPDATE CASCADE, 
	CONSTRAINT uq_scorecard_id_org_id_team_id UNIQUE (id, org_id, team_id), 
	CONSTRAINT uq_scorecard_attempt_id UNIQUE (attempt_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_scorecard_org_id ON scorecard (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_scorecard_team_id ON scorecard (team_id)
        """
    )

    # ── transcript_entry — review ──────────────────────────────
    op.execute(
        """
CREATE TABLE transcript_entry (
	attempt_id UUID NOT NULL, 
	seq INTEGER NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	speaker TEXT NOT NULL, 
	at_ms INTEGER NOT NULL, 
	text TEXT NOT NULL, 
	demeanor_label TEXT, 
	CONSTRAINT pk_transcript_entry PRIMARY KEY (attempt_id, seq), 
	CONSTRAINT ck_transcript_entry_speaker_valid CHECK (speaker in ('participant', 'buyer')), 
	CONSTRAINT ck_transcript_entry_at_ms_non_negative CHECK (at_ms >= 0), 
	CONSTRAINT ck_transcript_entry_demeanor_participant_only CHECK (speaker = 'participant' or demeanor_label is null), 
	CONSTRAINT fk_transcript_entry_attempt_id_org_id_team_id FOREIGN KEY(attempt_id, org_id, team_id) REFERENCES attempt (id, org_id, team_id) ON DELETE CASCADE ON UPDATE CASCADE
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_transcript_entry_org_id ON transcript_entry (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_transcript_entry_team_id ON transcript_entry (team_id)
        """
    )

    # ── dimension_score — review ───────────────────────────────
    op.execute(
        """
CREATE TABLE dimension_score (
	scorecard_id UUID NOT NULL, 
	rubric_dimension_id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	score NUMERIC(3, 1) NOT NULL, 
	note TEXT, 
	CONSTRAINT pk_dimension_score PRIMARY KEY (scorecard_id, rubric_dimension_id), 
	CONSTRAINT ck_dimension_score_score_range CHECK (score >= 0 and score <= 10), 
	CONSTRAINT fk_dimension_score_scorecard_id_org_id_team_id FOREIGN KEY(scorecard_id, org_id, team_id) REFERENCES scorecard (id, org_id, team_id) ON DELETE CASCADE ON UPDATE CASCADE, 
	CONSTRAINT fk_dimension_score_rubric_dimension_id FOREIGN KEY(rubric_dimension_id) REFERENCES rubric_dimension (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_dimension_score_org_id ON dimension_score (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_dimension_score_team_id ON dimension_score (team_id)
        """
    )

    # ── moment — review ────────────────────────────────────────
    op.execute(
        """
CREATE TABLE moment (
	id UUID NOT NULL, 
	org_id UUID NOT NULL, 
	team_id UUID NOT NULL, 
	scorecard_id UUID NOT NULL, 
	attempt_id UUID NOT NULL, 
	transcript_seq INTEGER, 
	at_ms INTEGER NOT NULL, 
	severity TEXT NOT NULL, 
	rubric_dimension_id UUID NOT NULL, 
	quote TEXT, 
	try_instead TEXT, 
	why_it_matters TEXT, 
	CONSTRAINT pk_moment PRIMARY KEY (id), 
	CONSTRAINT ck_moment_severity_valid CHECK (severity in ('green', 'amber', 'red')), 
	CONSTRAINT ck_moment_at_ms_non_negative CHECK (at_ms >= 0), 
	CONSTRAINT ck_moment_graded_moment_has_quote CHECK (severity = 'green' or quote is not null), 
	CONSTRAINT fk_moment_scorecard_id_org_id_team_id FOREIGN KEY(scorecard_id, org_id, team_id) REFERENCES scorecard (id, org_id, team_id) ON DELETE CASCADE ON UPDATE CASCADE, 
	CONSTRAINT fk_moment_attempt_id_transcript_seq FOREIGN KEY(attempt_id, transcript_seq) REFERENCES transcript_entry (attempt_id, seq), 
	CONSTRAINT fk_moment_rubric_dimension_id FOREIGN KEY(rubric_dimension_id) REFERENCES rubric_dimension (id)
)
        """
    )
    op.execute(
        """
CREATE INDEX idx_moment_scorecard ON moment (scorecard_id, severity)
        """
    )
    op.execute(
        """
CREATE INDEX ix_moment_org_id ON moment (org_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_moment_team_id ON moment (team_id)
        """
    )
