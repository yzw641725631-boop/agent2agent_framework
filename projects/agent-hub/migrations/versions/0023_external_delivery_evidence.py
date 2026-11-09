"""Immutable external observations tied to exact delivered checkpoints."""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE delivery_policies (
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        version integer NOT NULL CHECK(version>0), created_at timestamptz NOT NULL DEFAULT now(),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        PRIMARY KEY(repository_id,version),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE TABLE delivery_evidence (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id),
        repository_id uuid NOT NULL, task_id uuid NOT NULL, worklog_id uuid NOT NULL REFERENCES worklogs(id),
        delivery_id uuid NOT NULL REFERENCES code_deliveries(id),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        provider varchar(24) NOT NULL CHECK(provider='github'),
        kind varchar(24) NOT NULL CHECK(kind IN ('pull_request','ci','merge','deployment','rollback')),
        provider_id bigint NOT NULL CHECK(provider_id>0), git_commit varchar(64) NOT NULL,
        policy_version integer NOT NULL CHECK(policy_version>=0),
        repository_version integer NOT NULL CHECK(repository_version>0),
        successful boolean NOT NULL, observation jsonb NOT NULL,
        rollback_of_id uuid REFERENCES delivery_evidence(id),
        rollback_to_id uuid REFERENCES delivery_evidence(id),
        merge_evidence_id uuid REFERENCES delivery_evidence(id), receipt_hash varchar(64) NOT NULL,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
        CHECK((kind='rollback')=(rollback_of_id IS NOT NULL)),
        CHECK((kind='rollback')=(rollback_to_id IS NOT NULL)),
        CHECK(merge_evidence_id IS NULL OR kind IN ('ci','deployment','rollback'))
      );
      CREATE INDEX ix_delivery_evidence_checkpoint ON delivery_evidence(worklog_id,kind,created_at DESC,id);
      GRANT SELECT,INSERT ON delivery_policies,delivery_evidence TO hub_runtime;
    """)
    for table in ("delivery_policies", "delivery_evidence"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY owner_access ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY delivery_policy_read ON delivery_policies FOR SELECT TO hub_runtime USING(
        hub.has_repository_cap(project_id,repository_id,'code:read'));
      CREATE POLICY delivery_policy_insert ON delivery_policies FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_repository_cap(project_id,repository_id,'code:read')
        AND hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY delivery_evidence_read ON delivery_evidence FOR SELECT TO hub_runtime USING(
        hub.has_repository_cap(project_id,repository_id,'code:read')
        AND EXISTS(SELECT 1 FROM worklogs w
          WHERE w.id=delivery_evidence.worklog_id AND w.project_id=delivery_evidence.project_id));
      CREATE POLICY delivery_evidence_insert ON delivery_evidence FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_repository_cap(project_id,repository_id,'code:read')
        AND (hub.has_project_cap(project_id,'tasks:write') OR hub.has_project_cap(project_id,'tasks:publish'))
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM code_deliveries d JOIN worklogs w ON w.id=d.worklog_id
          WHERE d.id=delivery_id AND d.project_id=delivery_evidence.project_id
          AND d.repository_id=delivery_evidence.repository_id AND d.task_id=delivery_evidence.task_id
          AND d.worklog_id=delivery_evidence.worklog_id AND d.git_commit=delivery_evidence.git_commit));
    """)
    op.execute("""
      CREATE FUNCTION hub.guard_delivery_release() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$
      DECLARE
        config jsonb; config_version integer; repo_version integer; required_kind text;
        subject text; subjects jsonb; received delivery_evidence%ROWTYPE;
        merged delivery_evidence%ROWTYPE; delivered_commit text; target_commit text; cutoff timestamptz;
      BEGIN
        IF NEW.status<>'completed' OR OLD.status='completed' OR NEW.repository_id IS NULL THEN
          RETURN NEW;
        END IF;
        SELECT content,version INTO config,config_version FROM delivery_policies
          WHERE project_id=NEW.project_id AND repository_id=NEW.repository_id ORDER BY version DESC LIMIT 1;
        IF config IS NULL OR jsonb_array_length(config->'required_kinds')=0 THEN RETURN NEW; END IF;
        SELECT version INTO repo_version FROM project_repositories WHERE id=NEW.repository_id;
        SELECT git_commit INTO delivered_commit FROM code_deliveries WHERE worklog_id=NEW.submission_id;
        cutoff=now()-make_interval(secs=>(config->>'max_age_seconds')::integer);
        SELECT * INTO merged FROM delivery_evidence
          WHERE task_id=NEW.id AND worklog_id=NEW.submission_id AND kind='merge'
            AND policy_version=config_version AND repository_version=repo_version
          ORDER BY observation->>'observed_at' DESC,created_at DESC,id LIMIT 1;
        FOR required_kind IN SELECT jsonb_array_elements_text(config->'required_kinds') LOOP
          subjects=CASE WHEN required_kind='ci' THEN config->'workflows'
            WHEN required_kind IN ('deployment','rollback') THEN config->'environments'
            ELSE '[""]'::jsonb END;
          FOR subject IN SELECT jsonb_array_elements_text(subjects) LOOP
            target_commit=delivered_commit;
            IF required_kind IN ('ci','deployment','rollback') AND config->>(
              CASE WHEN required_kind='ci' THEN 'ci_target' ELSE 'deployment_target' END)='merged' THEN
              IF merged.id IS NULL OR NOT merged.successful
                OR (merged.observation->>'observed_at')::timestamptz<cutoff THEN
                RAISE EXCEPTION 'DELIVERY_EXTERNAL_EVIDENCE_REQUIRED' USING ERRCODE='23514';
              END IF;
              target_commit=merged.observation->>'merge_commit';
            END IF;
            SELECT * INTO received FROM delivery_evidence
              WHERE task_id=NEW.id AND worklog_id=NEW.submission_id AND kind=required_kind
                AND policy_version=config_version AND repository_version=repo_version
                AND coalesce(observation->>'workflow',observation->>'environment','')=subject
              ORDER BY observation->>'observed_at' DESC,created_at DESC,id LIMIT 1;
            IF received.id IS NULL OR NOT received.successful OR target_commit IS NULL
              OR received.observation->>'verified_commit' IS DISTINCT FROM target_commit
              OR (received.observation->>'observed_at')::timestamptz<cutoff THEN
              RAISE EXCEPTION 'DELIVERY_EXTERNAL_EVIDENCE_REQUIRED' USING ERRCODE='23514';
            END IF;
          END LOOP;
        END LOOP;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_delivery_release() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.guard_delivery_release() TO hub_runtime;
      CREATE TRIGGER delivery_release_guard BEFORE UPDATE OF status ON tasks
        FOR EACH ROW EXECUTE FUNCTION hub.guard_delivery_release();
    """)


def downgrade():
    raise RuntimeError("Preserve external evidence and policy revisions; restore a verified backup")
