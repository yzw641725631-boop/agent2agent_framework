"""Immutable guide amendments, independent review, publication and reader feedback."""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE guide_amendments (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        job_id uuid NOT NULL REFERENCES guide_jobs(id), base_version integer NOT NULL CHECK(base_version>=0),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, content_hash varchar(64) NOT NULL, reason varchar(2000) NOT NULL CHECK(length(trim(reason))>=10)
      );
      CREATE TABLE guide_amendment_reviews (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        amendment_id uuid NOT NULL REFERENCES guide_amendments(id),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        gate varchar(16) NOT NULL CHECK(gate IN ('product','test')), approved boolean NOT NULL,
        reason varchar(2000) NOT NULL CHECK(length(trim(reason))>=10), override_reason varchar(2000) NOT NULL DEFAULT '',
        UNIQUE(amendment_id,gate)
      );
      CREATE TABLE guide_publications (
        job_id uuid NOT NULL REFERENCES guide_jobs(id), version integer NOT NULL CHECK(version>0),
        created_at timestamptz NOT NULL DEFAULT now(), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), action varchar(16) NOT NULL CHECK(action IN ('published','archived','restored')),
        amendment_id uuid REFERENCES guide_amendments(id), reason varchar(2000) NOT NULL CHECK(length(trim(reason))>=10),
        PRIMARY KEY(job_id,version)
      );
      CREATE TABLE guide_feedback (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        job_id uuid NOT NULL REFERENCES guide_jobs(id), publication_version integer NOT NULL CHECK(publication_version>=0),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        kind varchar(16) NOT NULL CHECK(kind IN ('issue','suggestion','outdated')),
        body varchar(4000) NOT NULL CHECK(length(trim(body))>=10)
      );
      CREATE TABLE guide_feedback_resolutions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        feedback_id uuid NOT NULL REFERENCES guide_feedback(id),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        state varchar(16) NOT NULL CHECK(state IN ('resolved','rejected','open')),
        reason varchar(2000) NOT NULL CHECK(length(trim(reason))>=10)
      );
      CREATE FUNCTION hub.guide_readable(jid uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ SELECT EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=jid
          AND j.state='published' AND (hub.can_read_scope(j.project_id,j.repository_id)
          OR j.audience='project' AND hub.has_project_cap(j.project_id,'guides:read'))) $$;
      CREATE FUNCTION hub.guide_editable(jid uuid,cap text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ SELECT EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=jid
          AND j.state='published' AND hub.can_read_scope(j.project_id,j.repository_id)
          AND hub.has_project_cap(j.project_id,cap)
          AND (cap<>'tasks:write' OR j.repository_id IS NULL
            OR hub.has_repository_cap(j.project_id,j.repository_id,'code:write'))) $$;
      CREATE FUNCTION hub.guide_archived(jid uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ SELECT hub.guide_readable(jid) AND coalesce((SELECT action='archived'
          FROM guide_publications WHERE job_id=jid ORDER BY version DESC LIMIT 1),false) $$;
      REVOKE ALL ON FUNCTION hub.guide_readable(uuid),hub.guide_editable(uuid,text),hub.guide_archived(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.guide_readable(uuid),hub.guide_editable(uuid,text),hub.guide_archived(uuid) TO hub_runtime;
      GRANT SELECT,INSERT ON guide_amendments,guide_amendment_reviews,guide_publications,guide_feedback,guide_feedback_resolutions TO hub_runtime;
    """)
    for table in (
        "guide_amendments",
        "guide_amendment_reviews",
        "guide_publications",
        "guide_feedback",
        "guide_feedback_resolutions",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY owner_access ON {table} TO hub_owner USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY amendment_read ON guide_amendments FOR SELECT TO hub_runtime USING(
        hub.guide_editable(job_id,'project:read') OR hub.guide_readable(job_id) AND EXISTS(
          SELECT 1 FROM guide_publications p WHERE p.amendment_id=guide_amendments.id));
      CREATE POLICY amendment_insert ON guide_amendments FOR INSERT TO hub_runtime WITH CHECK(
        hub.guide_editable(job_id,'tasks:write') AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY amendment_review_read ON guide_amendment_reviews FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM guide_amendments a WHERE a.id=amendment_id AND hub.guide_editable(a.job_id,'project:read')));
      CREATE POLICY amendment_review_insert ON guide_amendment_reviews FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM guide_amendments a WHERE a.id=amendment_id
          AND hub.guide_editable(a.job_id,CASE WHEN gate='test' THEN 'tasks:test' ELSE 'tasks:publish' END)));
      CREATE POLICY publication_read ON guide_publications FOR SELECT TO hub_runtime USING(hub.guide_readable(job_id));
      CREATE POLICY publication_insert ON guide_publications FOR INSERT TO hub_runtime WITH CHECK(
        hub.guide_editable(job_id,'tasks:publish') AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY feedback_read ON guide_feedback FOR SELECT TO hub_runtime USING(hub.guide_readable(job_id) AND (
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid OR hub.guide_editable(job_id,'tasks:publish')));
      CREATE POLICY feedback_insert ON guide_feedback FOR INSERT TO hub_runtime WITH CHECK(hub.guide_readable(job_id)
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY resolution_read ON guide_feedback_resolutions FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM guide_feedback f WHERE f.id=feedback_id));
      CREATE POLICY resolution_insert ON guide_feedback_resolutions FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM guide_feedback f WHERE f.id=feedback_id AND hub.guide_editable(f.job_id,'tasks:publish')));
      CREATE FUNCTION hub.check_guide_amendment() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('guide-publication:'||NEW.job_id::text,0));
        IF NOT hub.guide_editable(NEW.job_id,'tasks:write') OR NEW.base_version<>coalesce((
          SELECT max(version) FROM guide_publications WHERE job_id=NEW.job_id),0)
          OR hub.guide_archived(NEW.job_id) THEN
          RAISE EXCEPTION 'stale or inaccessible guide amendment' USING ERRCODE='23514'; END IF; RETURN NEW;
      END $$;
      CREATE TRIGGER guide_amendment_valid BEFORE INSERT ON guide_amendments
        FOR EACH ROW EXECUTE FUNCTION hub.check_guide_amendment();
      CREATE FUNCTION hub.check_guide_amendment_review() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ DECLARE a guide_amendments; BEGIN
        SELECT * INTO a FROM guide_amendments WHERE id=NEW.amendment_id;
        PERFORM pg_advisory_xact_lock(hashtextextended('guide-publication:'||a.job_id::text,0));
        IF a.id IS NULL OR NOT hub.guide_editable(a.job_id,CASE WHEN NEW.gate='test' THEN 'tasks:test' ELSE 'tasks:publish' END)
          OR a.base_version<>coalesce((SELECT max(version) FROM guide_publications WHERE job_id=a.job_id),0)
          OR hub.guide_archived(a.job_id)
          OR a.author_id=NEW.author_id AND NOT (hub.is_superadmin() AND length(trim(NEW.override_reason))>=10)
          OR a.author_id<>NEW.author_id AND NEW.override_reason<>'' THEN
          RAISE EXCEPTION 'independent current guide review required' USING ERRCODE='23514'; END IF; RETURN NEW;
      END $$;
      CREATE TRIGGER guide_amendment_review_valid BEFORE INSERT ON guide_amendment_reviews
        FOR EACH ROW EXECUTE FUNCTION hub.check_guide_amendment_review();
      CREATE FUNCTION hub.check_guide_publication() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE previous guide_publications; current_version integer; a guide_amendments;
      BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('guide-publication:'||NEW.job_id::text,0));
        SELECT * INTO previous FROM guide_publications WHERE job_id=NEW.job_id ORDER BY version DESC LIMIT 1;
        current_version:=coalesce(previous.version,0);
        IF NOT hub.guide_editable(NEW.job_id,'tasks:publish') OR NEW.version<>current_version+1 THEN
          RAISE EXCEPTION 'stale or inaccessible guide publication' USING ERRCODE='23514'; END IF;
        IF NEW.action='published' THEN
          SELECT * INTO a FROM guide_amendments WHERE id=NEW.amendment_id AND job_id=NEW.job_id;
          IF a.id IS NULL OR a.base_version<>current_version OR coalesce(previous.action='archived',false)
            OR (SELECT count(DISTINCT gate) FROM guide_amendment_reviews WHERE amendment_id=a.id AND approved)<>2 THEN
            RAISE EXCEPTION 'guide amendment requires current independent approvals' USING ERRCODE='23514'; END IF;
        ELSE
          IF NEW.amendment_id IS DISTINCT FROM previous.amendment_id
            OR NEW.action='archived' AND previous.action='archived'
            OR NEW.action='restored' AND previous.action IS DISTINCT FROM 'archived' THEN
            RAISE EXCEPTION 'invalid guide archive or restore' USING ERRCODE='23514'; END IF;
        END IF; RETURN NEW;
      END $$;
      CREATE TRIGGER guide_publication_valid BEFORE INSERT ON guide_publications
        FOR EACH ROW EXECUTE FUNCTION hub.check_guide_publication();
      CREATE FUNCTION hub.check_guide_feedback() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('guide-publication:'||NEW.job_id::text,0));
        IF NOT hub.guide_readable(NEW.job_id) OR NEW.publication_version<>coalesce((
          SELECT max(version) FROM guide_publications WHERE job_id=NEW.job_id),0) THEN
          RAISE EXCEPTION 'feedback must reference the currently visible guide' USING ERRCODE='23514';
        END IF; RETURN NEW;
      END $$;
      CREATE TRIGGER guide_feedback_valid BEFORE INSERT ON guide_feedback
        FOR EACH ROW EXECUTE FUNCTION hub.check_guide_feedback();
      REVOKE ALL ON FUNCTION hub.check_guide_amendment(),hub.check_guide_amendment_review(),hub.check_guide_publication() FROM PUBLIC;
      REVOKE ALL ON FUNCTION hub.check_guide_feedback() FROM PUBLIC;
    """)


def downgrade():
    raise RuntimeError("Guide histories are retained; use a verified application rollback")
