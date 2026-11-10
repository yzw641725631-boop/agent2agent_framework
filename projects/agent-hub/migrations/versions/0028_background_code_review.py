"""Explicit code egress authorization and source-bound background audit provenance."""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE generation_calls ADD COLUMN source_receipt jsonb;
      ALTER TABLE generation_calls DROP CONSTRAINT generation_calls_kind_check;
      ALTER TABLE generation_calls ADD CONSTRAINT generation_calls_kind_check
        CHECK(kind IN ('distillation','guide','code_audit'));
      ALTER TABLE generation_receipts ADD COLUMN code_audit_id uuid REFERENCES code_audits(id);
      DO $migration$
      DECLARE constraint_name text;
      BEGIN
        SELECT conname INTO STRICT constraint_name FROM pg_constraint
          WHERE conrelid='public.generation_receipts'::regclass
          AND pg_get_constraintdef(oid) LIKE '%num_nonnulls%';
        EXECUTE format('ALTER TABLE public.generation_receipts DROP CONSTRAINT %I',constraint_name);
      END $migration$;
      ALTER TABLE generation_receipts ADD CONSTRAINT generation_receipt_one_source
        CHECK(num_nonnulls(distillation_id,guide_id,code_audit_id)=1);
      CREATE UNIQUE INDEX uq_generation_audit_epoch ON generation_receipts(code_audit_id,lease_epoch);
      DROP POLICY receipt_read ON generation_receipts;
      CREATE POLICY receipt_read ON generation_receipts FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND (
          distillation_id IS NOT NULL AND EXISTS(SELECT 1 FROM distillation_jobs j WHERE j.id=distillation_id)
          OR guide_id IS NOT NULL AND EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=guide_id)
          OR code_audit_id IS NOT NULL AND EXISTS(SELECT 1 FROM code_audits j WHERE j.id=code_audit_id)));
      DROP POLICY receipt_insert ON generation_receipts;
      CREATE POLICY receipt_insert ON generation_receipts FOR INSERT TO hub_generation WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND hub.can_read_scope(project_id,repository_id) AND (
          hub.has_project_cap(project_id,'tasks:write') AND (
            distillation_id IS NOT NULL AND EXISTS(SELECT 1 FROM distillation_jobs j
              WHERE j.id=distillation_id AND j.project_id=generation_receipts.project_id
              AND j.repository_id IS NOT DISTINCT FROM generation_receipts.repository_id
              AND j.target_user_id=author_id AND j.target_device_id=device_id
              AND j.lease_epoch=generation_receipts.lease_epoch AND j.state='succeeded')
            OR guide_id IS NOT NULL AND EXISTS(SELECT 1 FROM guide_jobs j
              WHERE j.id=guide_id AND j.project_id=generation_receipts.project_id
              AND j.repository_id IS NOT DISTINCT FROM generation_receipts.repository_id
              AND j.target_user_id=author_id AND j.target_device_id=device_id
              AND j.lease_epoch=generation_receipts.lease_epoch AND j.state='review'))
          OR hub.has_project_cap(project_id,'tasks:review') AND hub.has_project_cap(project_id,'logs:read')
            AND code_audit_id IS NOT NULL AND EXISTS(SELECT 1 FROM code_audits j
              WHERE j.id=code_audit_id AND j.project_id=generation_receipts.project_id
              AND j.repository_id=generation_receipts.repository_id
              AND j.reviewer_id=author_id AND j.device_id=generation_receipts.device_id
              AND j.lease_epoch=generation_receipts.lease_epoch AND j.state='completed')));
      DO $migration$
      DECLARE definition text; needle text := 'NEW.content->>''source_basis''=''exact_checkout''';
      BEGIN
        SELECT pg_get_functiondef('hub.guard_code_audit_report()'::regprocedure) INTO definition;
        IF length(definition)-length(replace(definition,needle,''))<>length(needle) THEN
          RAISE EXCEPTION 'Unexpected audit source validation definition'; END IF;
        EXECUTE replace(definition,needle,
          'NEW.content->>''source_basis'' IN (''exact_checkout'',''exact_git_objects'')');
      END $migration$;
    """)


def downgrade():
    raise RuntimeError("Retain provider and audit provenance; use a verified application rollback")
