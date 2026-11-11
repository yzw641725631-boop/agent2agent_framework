-- Fresh server volume only. Object owner is the separate migration role.
CREATE EXTENSION IF NOT EXISTS vector;
SET ROLE hub_owner;
BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE TABLE outbox_events (
    kind VARCHAR(80) NOT NULL, 
    payload JSONB NOT NULL, 
    state VARCHAR(24) NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id)
);

CREATE TABLE proof_replays (
    thumbprint VARCHAR(64) NOT NULL, 
    jti VARCHAR(128) NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (thumbprint, jti)
);

CREATE INDEX ix_proof_replays_expires_at ON proof_replays (expires_at);

CREATE TABLE rate_buckets (
    key VARCHAR(64) NOT NULL, 
    count INTEGER NOT NULL, 
    resets_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (key)
);

CREATE INDEX ix_rate_buckets_resets_at ON rate_buckets (resets_at);

CREATE TABLE users (
    login VARCHAR(80) NOT NULL, 
    password_hash TEXT NOT NULL, 
    platform_role VARCHAR(24) NOT NULL, 
    admin_eligible BOOLEAN NOT NULL, 
    email VARCHAR(254), 
    email_verified_at TIMESTAMP WITH TIME ZONE, 
    status VARCHAR(24) NOT NULL, 
    auth_epoch INTEGER NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (email), 
    UNIQUE (login)
);

CREATE UNIQUE INDEX one_superadministrator ON users (platform_role) WHERE platform_role = 'superadmin';

CREATE TABLE approval_requests (
    kind VARCHAR(24) NOT NULL, 
    requester_id UUID NOT NULL, 
    subject_id UUID NOT NULL, 
    payload JSONB NOT NULL, 
    payload_hash VARCHAR(64) NOT NULL, 
    version INTEGER NOT NULL, 
    status VARCHAR(24) NOT NULL, 
    decided_by UUID, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(decided_by) REFERENCES users (id), 
    FOREIGN KEY(requester_id) REFERENCES users (id), 
    FOREIGN KEY(subject_id) REFERENCES users (id)
);

CREATE TABLE devices (
    user_id UUID NOT NULL, 
    public_jwk JSONB NOT NULL, 
    thumbprint VARCHAR(64) NOT NULL, 
    name VARCHAR(100) NOT NULL, 
    os VARCHAR(16) NOT NULL, 
    status VARCHAR(24) NOT NULL, 
    email_verified_at TIMESTAMP WITH TIME ZONE, 
    approved_by UUID, 
    epoch INTEGER NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(approved_by) REFERENCES users (id), 
    FOREIGN KEY(user_id) REFERENCES users (id), 
    UNIQUE (thumbprint)
);

CREATE INDEX ix_devices_user_id ON devices (user_id);

CREATE TABLE idempotency_records (
    actor_id UUID NOT NULL, 
    scope VARCHAR(300) NOT NULL, 
    key VARCHAR(128) NOT NULL, 
    payload_hash VARCHAR(64) NOT NULL, 
    result JSONB NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(actor_id) REFERENCES users (id), 
    UNIQUE (actor_id, scope, key)
);

CREATE TABLE projects (
    slug VARCHAR(80) NOT NULL, 
    name VARCHAR(200) NOT NULL, 
    created_by UUID NOT NULL, 
    status VARCHAR(24) NOT NULL, 
    version INTEGER NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(created_by) REFERENCES users (id), 
    UNIQUE (slug)
);

CREATE TABLE access_grants (
    project_id UUID NOT NULL, 
    user_id UUID NOT NULL, 
    device_id UUID NOT NULL, 
    approval_id UUID NOT NULL, 
    capabilities JSONB NOT NULL, 
    membership_epoch INTEGER NOT NULL, 
    user_epoch INTEGER NOT NULL, 
    device_epoch INTEGER NOT NULL, 
    status VARCHAR(24) NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(approval_id) REFERENCES approval_requests (id), 
    FOREIGN KEY(device_id) REFERENCES devices (id), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(user_id) REFERENCES users (id), 
    UNIQUE (approval_id)
);

CREATE INDEX ix_access_grants_project_id ON access_grants (project_id);

CREATE TABLE audit_events (
    actor_id UUID, 
    project_id UUID, 
    action VARCHAR(100) NOT NULL, 
    object_id VARCHAR(100) NOT NULL, 
    request_id VARCHAR(64) NOT NULL, 
    details JSONB NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(actor_id) REFERENCES users (id), 
    FOREIGN KEY(project_id) REFERENCES projects (id)
);

CREATE INDEX ix_audit_events_project_id ON audit_events (project_id);

CREATE TABLE email_challenges (
    device_id UUID NOT NULL, 
    email VARCHAR(254) NOT NULL, 
    digest VARCHAR(64) NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    attempts INTEGER NOT NULL, 
    consumed_at TIMESTAMP WITH TIME ZONE, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(device_id) REFERENCES devices (id)
);

CREATE INDEX ix_email_challenges_device_id ON email_challenges (device_id);

CREATE TABLE enrollments (
    device_id UUID NOT NULL, 
    digest VARCHAR(64) NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(device_id) REFERENCES devices (id), 
    UNIQUE (digest)
);

CREATE TABLE project_memberships (
    project_id UUID NOT NULL, 
    user_id UUID NOT NULL, 
    role VARCHAR(24) NOT NULL, 
    status VARCHAR(24) NOT NULL, 
    epoch INTEGER NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(user_id) REFERENCES users (id), 
    UNIQUE (project_id, user_id)
);

CREATE INDEX ix_project_memberships_project_id ON project_memberships (project_id);

CREATE INDEX ix_project_memberships_user_id ON project_memberships (user_id);

CREATE TABLE auth_sessions (
    user_id UUID NOT NULL, 
    device_id UUID NOT NULL, 
    grant_id UUID, 
    family_id UUID NOT NULL, 
    access_digest VARCHAR(64) NOT NULL, 
    refresh_digest VARCHAR(64) NOT NULL, 
    user_epoch INTEGER NOT NULL, 
    device_epoch INTEGER NOT NULL, 
    state VARCHAR(24) NOT NULL, 
    access_expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    refresh_expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(device_id) REFERENCES devices (id), 
    FOREIGN KEY(grant_id) REFERENCES access_grants (id), 
    FOREIGN KEY(user_id) REFERENCES users (id), 
    UNIQUE (access_digest), 
    UNIQUE (refresh_digest)
);

CREATE INDEX ix_auth_sessions_family_id ON auth_sessions (family_id);

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

-- Running upgrade 0001 -> 0002

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA hub;

REVOKE ALL ON SCHEMA hub FROM PUBLIC;

GRANT USAGE ON SCHEMA public, hub TO hub_runtime;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hub_runtime;

REVOKE ALL ON alembic_version FROM hub_runtime;

REVOKE UPDATE, DELETE ON audit_events FROM hub_runtime;

CREATE FUNCTION hub.is_superadmin() RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT EXISTS (
            SELECT 1 FROM public.users u JOIN public.devices d ON d.user_id = u.id
            WHERE u.id = nullif(current_setting('hub.user_id', true), '')::uuid
              AND d.id = nullif(current_setting('hub.device_id', true), '')::uuid
              AND u.platform_role = 'superadmin' AND u.status = 'active'
              AND u.email_verified_at IS NOT NULL AND d.status = 'approved'
              AND d.email_verified_at IS NOT NULL
          )
        $$;

CREATE FUNCTION hub.can_access_project(pid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.users u
            JOIN public.devices d ON d.user_id = u.id
            JOIN public.project_memberships m ON m.user_id = u.id
            JOIN public.access_grants g ON g.project_id = m.project_id AND g.user_id = u.id
            WHERE u.id = nullif(current_setting('hub.user_id', true), '')::uuid
              AND d.id = nullif(current_setting('hub.device_id', true), '')::uuid
              AND g.id = nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.device_id = d.id AND m.project_id = pid
              AND u.status = 'active' AND d.status = 'approved' AND m.status = 'active'
              AND (u.email IS NULL OR (u.email_verified_at IS NOT NULL AND d.email_verified_at IS NOT NULL))
              AND g.status = 'active' AND g.expires_at > now()
              AND g.membership_epoch = m.epoch AND g.user_epoch = u.auth_epoch AND g.device_epoch = d.epoch
              AND g.capabilities ? 'project:read'
          )
        $$;

REVOKE ALL ON FUNCTION hub.is_superadmin(), hub.can_access_project(uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION hub.is_superadmin(), hub.can_access_project(uuid) TO hub_runtime;

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;

ALTER TABLE projects FORCE ROW LEVEL SECURITY;

CREATE POLICY project_read ON projects FOR SELECT USING (hub.is_superadmin() OR (status='active' AND hub.can_access_project(id)));

CREATE POLICY project_insert ON projects FOR INSERT WITH CHECK (hub.is_superadmin());

CREATE POLICY project_update ON projects FOR UPDATE USING (hub.is_superadmin()) WITH CHECK (hub.is_superadmin());

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;

CREATE POLICY audit_insert ON audit_events FOR INSERT WITH CHECK (true);

CREATE POLICY audit_read ON audit_events FOR SELECT USING (
          hub.is_superadmin() OR (
            project_id IS NOT NULL AND hub.can_access_project(project_id)
            AND EXISTS (SELECT 1 FROM public.access_grants g
                        WHERE g.id = nullif(current_setting('hub.grant_id', true), '')::uuid
                          AND g.project_id = audit_events.project_id AND g.capabilities ? 'audit:read')
          )
        );

UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001';

-- Running upgrade 0002 -> 0003

ALTER TABLE email_challenges ADD COLUMN purpose VARCHAR(16) DEFAULT 'device' NOT NULL;

ALTER TABLE email_challenges ADD COLUMN user_epoch INTEGER;

CREATE TABLE site_configuration (
    key VARCHAR(40) NOT NULL, 
    encrypted_value TEXT NOT NULL, 
    version INTEGER NOT NULL, 
    updated_by UUID, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (key), 
    FOREIGN KEY(updated_by) REFERENCES users (id)
);

CREATE TABLE bootstrap_attempts (
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    login VARCHAR(80) NOT NULL, 
    password_hash TEXT NOT NULL, 
    email VARCHAR(254) NOT NULL, 
    public_jwk JSONB NOT NULL, 
    thumbprint VARCHAR(64) NOT NULL, 
    device_name VARCHAR(100) NOT NULL, 
    os VARCHAR(16) NOT NULL, 
    configuration_version INTEGER NOT NULL, 
    challenge_digest VARCHAR(64) NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    attempts INTEGER NOT NULL, 
    PRIMARY KEY (id)
);

CREATE INDEX ix_bootstrap_attempts_thumbprint ON bootstrap_attempts (thumbprint);

GRANT SELECT, INSERT, UPDATE, DELETE ON site_configuration, bootstrap_attempts TO hub_runtime;

UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002';

-- Running upgrade 0003 -> 0004

CREATE TABLE project_progress (
    sequence BIGINT GENERATED BY DEFAULT AS IDENTITY, 
    project_id UUID NOT NULL, 
    author_id UUID NOT NULL, 
    device_id UUID NOT NULL, 
    status VARCHAR(24) NOT NULL, 
    summary VARCHAR(300) NOT NULL, 
    detail TEXT NOT NULL, 
    task_reference VARCHAR(128) NOT NULL, 
    git_commit VARCHAR(64), 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (sequence), 
    CHECK (status IN ('running','checkpoint','blocked','completed')), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(author_id) REFERENCES users (id), 
    FOREIGN KEY(device_id) REFERENCES devices (id)
);

CREATE INDEX ix_project_progress_project_id ON project_progress (project_id, sequence);

GRANT SELECT, INSERT ON project_progress TO hub_runtime;

GRANT USAGE ON SEQUENCE project_progress_sequence_seq TO hub_runtime;

ALTER TABLE project_progress ENABLE ROW LEVEL SECURITY;

ALTER TABLE project_progress FORCE ROW LEVEL SECURITY;

CREATE POLICY progress_read ON project_progress FOR SELECT USING (hub.can_access_project(project_id));

CREATE POLICY progress_insert ON project_progress FOR INSERT WITH CHECK (
          hub.can_access_project(project_id)
          AND author_id = nullif(current_setting('hub.user_id', true), '')::uuid
          AND device_id = nullif(current_setting('hub.device_id', true), '')::uuid
          AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g
            WHERE g.id = nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.project_id = project_progress.project_id AND g.capabilities ? 'tasks:write'
          ))
        );

UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0003';

-- Running upgrade 0004 -> 0005

CREATE FUNCTION hub.can_read_documents(pid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g
            WHERE g.id=nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.project_id=pid AND g.capabilities ? 'documents:read'
          ))
        $$;
        CREATE FUNCTION hub.can_manage_documents(pid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g JOIN public.project_memberships m
              ON m.project_id=g.project_id AND m.user_id=g.user_id
            WHERE g.id=nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.project_id=pid AND g.capabilities ? 'documents:manage'
              AND m.role='administrator' AND m.status='active'
          ))
        $$;
        REVOKE ALL ON FUNCTION hub.can_read_documents(uuid), hub.can_manage_documents(uuid) FROM PUBLIC;
        GRANT USAGE ON SCHEMA public, hub TO hub_indexer;
        GRANT EXECUTE ON FUNCTION hub.can_read_documents(uuid), hub.can_manage_documents(uuid)
          TO hub_runtime, hub_indexer;
        GRANT EXECUTE ON FUNCTION hub.is_superadmin(), hub.can_access_project(uuid) TO hub_indexer;
        CREATE TABLE document_repositories (
          project_id uuid PRIMARY KEY REFERENCES projects(id), repository_url varchar(400) NOT NULL,
          branch varchar(200) NOT NULL, include_paths jsonb NOT NULL, encrypted_token text,
          version integer NOT NULL CHECK (version>0), snapshot_id uuid, git_commit varchar(64),
          indexed_at timestamptz, document_count integer NOT NULL DEFAULT 0,
          skipped_count integer NOT NULL DEFAULT 0
        );
        CREATE TABLE document_syncs (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), configuration_version integer NOT NULL,
          requested_by uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          grant_id uuid REFERENCES access_grants(id), status varchar(20) NOT NULL DEFAULT 'queued',
          attempts integer NOT NULL DEFAULT 0, lease_until timestamptz, lease_id uuid,
          finished_at timestamptz, error_code varchar(80),
          CHECK (status IN ('queued','running','succeeded','failed','cancelled'))
        );
        CREATE INDEX ix_document_syncs_project ON document_syncs(project_id, created_at DESC);
        CREATE UNIQUE INDEX one_document_sync_active ON document_syncs(project_id)
          WHERE status IN ('queued','running');
        CREATE TABLE project_documents (
          project_id uuid NOT NULL REFERENCES projects(id), path varchar(800) NOT NULL,
          snapshot_id uuid NOT NULL, title varchar(300) NOT NULL, content text NOT NULL,
          sha256 varchar(64) NOT NULL, byte_size integer NOT NULL, PRIMARY KEY(project_id, path)
        );
        GRANT SELECT, INSERT, UPDATE, DELETE ON document_repositories, document_syncs TO hub_runtime;
        GRANT SELECT, DELETE ON project_documents TO hub_runtime;
        GRANT SELECT, UPDATE ON document_repositories, document_syncs TO hub_indexer;
        GRANT SELECT, INSERT, DELETE ON project_documents TO hub_indexer;
        GRANT SELECT ON projects, users, devices, project_memberships, access_grants TO hub_indexer;
        GRANT INSERT ON audit_events TO hub_indexer;
        CREATE POLICY project_indexer ON projects FOR SELECT TO hub_indexer USING (true);;

ALTER TABLE document_repositories ENABLE ROW LEVEL SECURITY;

ALTER TABLE document_repositories FORCE ROW LEVEL SECURITY;

CREATE POLICY indexer ON document_repositories TO hub_indexer USING (true) WITH CHECK (true);

ALTER TABLE document_syncs ENABLE ROW LEVEL SECURITY;

ALTER TABLE document_syncs FORCE ROW LEVEL SECURITY;

CREATE POLICY indexer ON document_syncs TO hub_indexer USING (true) WITH CHECK (true);

ALTER TABLE project_documents ENABLE ROW LEVEL SECURITY;

ALTER TABLE project_documents FORCE ROW LEVEL SECURITY;

CREATE POLICY indexer ON project_documents TO hub_indexer USING (true) WITH CHECK (true);

CREATE POLICY repository_read ON document_repositories FOR SELECT TO hub_runtime
          USING (hub.can_read_documents(project_id) OR hub.can_manage_documents(project_id));
        CREATE POLICY repository_manage ON document_repositories FOR ALL TO hub_runtime
          USING (hub.can_manage_documents(project_id)) WITH CHECK (hub.can_manage_documents(project_id));
        CREATE POLICY sync_manage ON document_syncs FOR ALL TO hub_runtime
          USING (hub.can_manage_documents(project_id)) WITH CHECK (hub.can_manage_documents(project_id));
        CREATE POLICY document_read ON project_documents FOR SELECT TO hub_runtime
          USING (hub.can_read_documents(project_id) OR hub.can_manage_documents(project_id));
        CREATE POLICY document_clear ON project_documents FOR DELETE TO hub_runtime
          USING (hub.can_manage_documents(project_id));;

UPDATE alembic_version SET version_num='0005' WHERE alembic_version.version_num = '0004';

-- Running upgrade 0005 -> 0006

UPDATE approval_requests SET status='expired'
          WHERE status='pending_approval' AND expires_at <= now();
        DELETE FROM rate_buckets WHERE resets_at < now();
        CREATE INDEX ix_approval_pending ON approval_requests(created_at, id)
          WHERE status='pending_approval';
        CREATE INDEX ix_mail_outbox_pending ON outbox_events(created_at)
          WHERE kind='mail.delivery' AND state IN ('pending', 'running');;

UPDATE alembic_version SET version_num='0006' WHERE alembic_version.version_num = '0005';

-- Running upgrade 0006 -> 0007

CREATE FUNCTION hub.has_project_cap(pid uuid, cap text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g
            WHERE g.id=nullif(current_setting('hub.grant_id', true), '')::uuid
              AND g.project_id=pid AND g.capabilities ? cap
          ))
        $$;
        REVOKE ALL ON FUNCTION hub.has_project_cap(uuid,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION hub.has_project_cap(uuid,text) TO hub_runtime;
        CREATE TABLE tasks (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), parent_id uuid,
          created_by uuid NOT NULL REFERENCES users(id), title varchar(200) NOT NULL,
          description text NOT NULL, acceptance text NOT NULL, scope jsonb NOT NULL,
          status varchar(24) NOT NULL DEFAULT 'draft', admission_step integer NOT NULL DEFAULT 0,
          version integer NOT NULL DEFAULT 1, lease_epoch integer NOT NULL DEFAULT 0,
          active_run_id uuid, lease_until timestamptz, submission_id uuid,
          UNIQUE(id,project_id), FOREIGN KEY(parent_id,project_id) REFERENCES tasks(id,project_id),
          CHECK (status IN ('draft','ready','in_progress','review','testing','release_ready','completed')),
          CHECK (admission_step BETWEEN 0 AND 3 AND version>0 AND lease_epoch>=0),
          CHECK ((active_run_id IS NULL) = (lease_until IS NULL))
        );
        CREATE INDEX ix_tasks_project_created ON tasks(project_id,created_at,id);
        CREATE TABLE task_runs (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL,
          user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          instance_id uuid NOT NULL, harness varchar(24) NOT NULL, role varchar(24) NOT NULL,
          lease_epoch integer NOT NULL, state varchar(24) NOT NULL DEFAULT 'active', ended_at timestamptz,
          UNIQUE(id,task_id,project_id), FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
          CHECK (role IN ('developer','reviewer','tester','publisher')),
          CHECK (state IN ('active','submitted','released','expired','finished'))
        );
        CREATE INDEX ix_task_runs_task ON task_runs(project_id,task_id,created_at);
        CREATE UNIQUE INDEX one_active_developer ON task_runs(task_id) WHERE role='developer' AND state='active';
        CREATE TABLE worklogs (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL, run_id uuid NOT NULL,
          author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          lease_epoch integer NOT NULL, summary varchar(300) NOT NULL, notes text NOT NULL,
          changed_paths jsonb NOT NULL, tests text NOT NULL, next_steps text NOT NULL,
          outcome varchar(24) NOT NULL, git_commit varchar(64), receipt_hash varchar(64) NOT NULL,
          FOREIGN KEY(run_id,task_id,project_id) REFERENCES task_runs(id,task_id,project_id),
          CHECK (outcome IN ('checkpoint','blocked','failed'))
        );
        CREATE INDEX ix_worklogs_task ON worklogs(project_id,task_id,created_at,id);
        CREATE TABLE task_decisions (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL,
          author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          run_id uuid, worklog_id uuid, gate varchar(24) NOT NULL, approved boolean NOT NULL,
          evidence text NOT NULL, override_reason text NOT NULL, task_version integer NOT NULL,
          FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
          CHECK (gate IN ('requirements','design','feasibility','review','test','release'))
        );
        CREATE INDEX ix_task_decisions_task ON task_decisions(project_id,task_id,created_at);
        ALTER TABLE project_progress ADD COLUMN kind varchar(40) NOT NULL DEFAULT 'report';
        ALTER TABLE project_progress ADD COLUMN task_id uuid;
        ALTER TABLE project_progress ADD COLUMN run_id uuid;
        ALTER TABLE project_progress ADD COLUMN worklog_id uuid;
        GRANT SELECT,INSERT,UPDATE ON tasks,task_runs TO hub_runtime;
        GRANT SELECT,INSERT ON worklogs,task_decisions TO hub_runtime;;

ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;

ALTER TABLE tasks FORCE ROW LEVEL SECURITY;

ALTER TABLE task_runs ENABLE ROW LEVEL SECURITY;

ALTER TABLE task_runs FORCE ROW LEVEL SECURITY;

ALTER TABLE worklogs ENABLE ROW LEVEL SECURITY;

ALTER TABLE worklogs FORCE ROW LEVEL SECURITY;

ALTER TABLE task_decisions ENABLE ROW LEVEL SECURITY;

ALTER TABLE task_decisions FORCE ROW LEVEL SECURITY;

CREATE POLICY tasks_read ON tasks FOR SELECT TO hub_runtime USING (hub.can_access_project(project_id));
        CREATE POLICY tasks_insert ON tasks FOR INSERT TO hub_runtime WITH CHECK (
          hub.has_project_cap(project_id,'tasks:publish')
          AND created_by=nullif(current_setting('hub.user_id',true),'')::uuid);
        CREATE POLICY tasks_update ON tasks FOR UPDATE TO hub_runtime
          USING (hub.has_project_cap(project_id,'tasks:publish') OR hub.has_project_cap(project_id,'tasks:claim')
            OR hub.has_project_cap(project_id,'tasks:write') OR hub.has_project_cap(project_id,'tasks:review')
            OR hub.has_project_cap(project_id,'tasks:test'))
          WITH CHECK (hub.can_access_project(project_id));
        CREATE POLICY runs_read ON task_runs FOR SELECT TO hub_runtime USING (hub.can_access_project(project_id));
        CREATE POLICY runs_insert ON task_runs FOR INSERT TO hub_runtime WITH CHECK (
          user_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND ((role='developer' AND hub.has_project_cap(project_id,'tasks:claim'))
            OR (role='reviewer' AND hub.has_project_cap(project_id,'tasks:review'))
            OR (role='tester' AND hub.has_project_cap(project_id,'tasks:test'))
            OR (role='publisher' AND hub.has_project_cap(project_id,'tasks:publish'))));
        CREATE POLICY runs_update ON task_runs FOR UPDATE TO hub_runtime
          USING (hub.has_project_cap(project_id,'tasks:claim') OR hub.has_project_cap(project_id,'tasks:write'))
          WITH CHECK (hub.can_access_project(project_id));
        CREATE POLICY logs_read ON worklogs FOR SELECT TO hub_runtime USING (
          hub.can_access_project(project_id) AND (hub.has_project_cap(project_id,'logs:read')
          OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid));
        CREATE POLICY logs_insert ON worklogs FOR INSERT TO hub_runtime WITH CHECK (
          hub.has_project_cap(project_id,'tasks:write')
          AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND EXISTS (SELECT 1 FROM tasks t JOIN task_runs r ON r.id=t.active_run_id
            WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id AND t.status='in_progress'
              AND t.lease_until>clock_timestamp() AND t.lease_epoch=worklogs.lease_epoch
              AND r.id=worklogs.run_id AND r.user_id=worklogs.author_id AND r.device_id=worklogs.device_id
              AND r.state='active'));
        CREATE POLICY decisions_read ON task_decisions FOR SELECT TO hub_runtime USING (
          hub.can_access_project(project_id) AND (hub.has_project_cap(project_id,'logs:read')
          OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid));
        CREATE POLICY decisions_insert ON task_decisions FOR INSERT TO hub_runtime WITH CHECK (
          author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND ((gate='review' AND hub.has_project_cap(project_id,'tasks:review'))
            OR (gate='test' AND hub.has_project_cap(project_id,'tasks:test'))
            OR (gate IN ('requirements','design','feasibility','release') AND hub.has_project_cap(project_id,'tasks:publish'))));
        DROP POLICY progress_insert ON project_progress;
        CREATE POLICY progress_insert ON project_progress FOR INSERT WITH CHECK (
          author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND (hub.has_project_cap(project_id,'tasks:write')
            OR (kind<>'report' AND (hub.has_project_cap(project_id,'tasks:publish')
              OR hub.has_project_cap(project_id,'tasks:claim') OR hub.has_project_cap(project_id,'tasks:review')
              OR hub.has_project_cap(project_id,'tasks:test')))));;

UPDATE alembic_version SET version_num='0007' WHERE alembic_version.version_num = '0006';

-- Running upgrade 0007 -> 0008

ALTER TABLE tasks ADD COLUMN execution_mode varchar(24) NOT NULL DEFAULT 'declaration'
          CHECK (execution_mode IN ('observed','declaration'));
        ALTER TABLE worklogs ADD COLUMN context_id uuid;
        ALTER TABLE worklogs ADD COLUMN change_evidence jsonb;
        ALTER TABLE worklogs ADD COLUMN evidence_hash varchar(64);
        CREATE TABLE project_policies (
          project_id uuid NOT NULL REFERENCES projects(id), version integer NOT NULL CHECK(version>0),
          author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(project_id,version)
        );
        CREATE TABLE work_contexts (
          id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
          project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL, run_id uuid NOT NULL,
          author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
          lease_epoch integer NOT NULL CHECK(lease_epoch>0), policy_version integer NOT NULL CHECK(policy_version>=0),
          task_hash varchar(64) NOT NULL, payload jsonb NOT NULL, content_hash varchar(64) NOT NULL,
          acknowledged_at timestamptz,
          FOREIGN KEY(run_id,task_id,project_id) REFERENCES task_runs(id,task_id,project_id)
        );
        CREATE INDEX ix_contexts_run ON work_contexts(project_id,run_id,created_at);
        GRANT SELECT,INSERT ON project_policies TO hub_runtime;
        GRANT SELECT,INSERT ON work_contexts TO hub_runtime;
        GRANT UPDATE(acknowledged_at) ON work_contexts TO hub_runtime;
        ALTER TABLE project_policies ENABLE ROW LEVEL SECURITY;
        ALTER TABLE project_policies FORCE ROW LEVEL SECURITY;
        ALTER TABLE work_contexts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE work_contexts FORCE ROW LEVEL SECURITY;
        CREATE POLICY policies_read ON project_policies FOR SELECT TO hub_runtime
          USING (hub.can_access_project(project_id));
        CREATE POLICY policies_insert ON project_policies FOR INSERT TO hub_runtime WITH CHECK (
          hub.has_project_cap(project_id,'tasks:publish')
          AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
        CREATE POLICY contexts_read ON work_contexts FOR SELECT TO hub_runtime USING (
          hub.can_access_project(project_id) AND (hub.has_project_cap(project_id,'logs:read')
            OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid));
        CREATE POLICY contexts_insert ON work_contexts FOR INSERT TO hub_runtime WITH CHECK (
          hub.has_project_cap(project_id,'tasks:write')
          AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND EXISTS (SELECT 1 FROM tasks t JOIN task_runs r ON t.active_run_id=r.id
            WHERE t.id=work_contexts.task_id AND t.project_id=work_contexts.project_id
              AND r.id=work_contexts.run_id AND r.state='active' AND t.lease_epoch=work_contexts.lease_epoch
              AND t.lease_until>clock_timestamp() AND r.user_id=work_contexts.author_id AND r.device_id=work_contexts.device_id));
        CREATE POLICY contexts_ack ON work_contexts FOR UPDATE TO hub_runtime USING (
          hub.has_project_cap(project_id,'tasks:write')
          AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid)
          WITH CHECK (hub.can_access_project(project_id));;

UPDATE alembic_version SET version_num='0008' WHERE alembic_version.version_num = '0007';

-- Running upgrade 0008 -> 0009

ALTER TABLE projects ADD COLUMN repository_mode varchar(16) NOT NULL DEFAULT 'legacy'
        CHECK(repository_mode IN ('legacy','scoped'));
      ALTER TABLE access_grants ADD COLUMN repository_permissions jsonb NOT NULL DEFAULT '[]'
        CHECK(jsonb_typeof(repository_permissions)='array');
      CREATE TABLE project_repositories (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), slug varchar(80) NOT NULL,
        name varchar(200) NOT NULL, platform varchar(24) NOT NULL,
        repository_url varchar(400) NOT NULL, branch varchar(200) NOT NULL,
        auth_mode varchar(24) NOT NULL CHECK(auth_mode IN ('public','token','github_app')),
        encrypted_token text, installation_id bigint,
        status varchar(16) NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
        version integer NOT NULL DEFAULT 1 CHECK(version>0),
        UNIQUE(project_id,slug), UNIQUE(id,project_id), UNIQUE(project_id,repository_url)
      );
      CREATE INDEX ix_project_repositories_project_id ON project_repositories(project_id);
      ALTER TABLE project_repositories ENABLE ROW LEVEL SECURITY;
      ALTER TABLE project_repositories FORCE ROW LEVEL SECURITY;
      GRANT SELECT,INSERT,UPDATE ON project_repositories TO hub_runtime;
      GRANT SELECT ON project_repositories TO hub_indexer;
      -- Definer lookups see metadata without recursively invoking the runtime policy.
      CREATE POLICY repository_owner ON project_repositories TO hub_owner USING(true) WITH CHECK(true);
      CREATE POLICY repository_indexer ON project_repositories FOR SELECT TO hub_indexer USING(true);
      CREATE FUNCTION hub.has_repository_cap(pid uuid, rid uuid, cap text) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND EXISTS (
          SELECT 1 FROM public.project_repositories r WHERE r.id=rid AND r.project_id=pid
          AND r.status='active' AND (hub.is_superadmin() OR EXISTS (
            SELECT 1 FROM public.access_grants g
            WHERE g.id=nullif(current_setting('hub.grant_id',true),'')::uuid AND g.project_id=pid
            AND g.repository_permissions @> jsonb_build_array(jsonb_build_object(
              'repository_id',rid::text,'capabilities',jsonb_build_array(cap)))
          ))
        )
      $$;
      CREATE FUNCTION hub.can_read_scope(pid uuid, rid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR
          CASE WHEN rid IS NOT NULL THEN hub.has_repository_cap(pid,rid,'code:read')
          ELSE EXISTS(SELECT 1 FROM public.projects p WHERE p.id=pid AND p.repository_mode='legacy') END)
      $$;
      CREATE FUNCTION hub.can_read_source(pid uuid, sid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND (hub.is_superadmin() OR
          CASE WHEN sid=pid THEN hub.can_read_scope(pid,NULL)
          ELSE hub.has_repository_cap(pid,sid,'documents:read') OR
            (hub.can_manage_documents(pid) AND hub.has_repository_cap(pid,sid,'code:read')) END)
      $$;
      REVOKE ALL ON FUNCTION hub.has_repository_cap(uuid,uuid,text), hub.can_read_scope(uuid,uuid),
        hub.can_read_source(uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.has_repository_cap(uuid,uuid,text), hub.can_read_scope(uuid,uuid),
        hub.can_read_source(uuid,uuid), hub.has_project_cap(uuid,text) TO hub_runtime,hub_indexer;
      CREATE POLICY repository_read ON project_repositories FOR SELECT TO hub_runtime USING (
        hub.has_project_cap(project_id,'repositories:manage') OR
        hub.has_project_cap(project_id,'grants:request') OR
        hub.has_repository_cap(project_id,id,'code:read') OR
        hub.has_repository_cap(project_id,id,'documents:read'));
      CREATE POLICY repository_insert ON project_repositories FOR INSERT TO hub_runtime
        WITH CHECK(hub.has_project_cap(project_id,'repositories:manage'));
      CREATE POLICY repository_update ON project_repositories FOR UPDATE TO hub_runtime
        USING(hub.has_project_cap(project_id,'repositories:manage'))
        WITH CHECK(hub.has_project_cap(project_id,'repositories:manage'));
      -- Mode activation is a one-way operation; runtime cannot arbitrarily update projects.
      CREATE FUNCTION hub.activate_repository_scope(pid uuid) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF NOT hub.has_project_cap(pid,'repositories:manage') THEN RAISE EXCEPTION 'permission denied'; END IF;
        UPDATE public.projects SET repository_mode='scoped', version=version+1
          WHERE id=pid AND repository_mode='legacy';
      END $$;
      REVOKE ALL ON FUNCTION hub.activate_repository_scope(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.activate_repository_scope(uuid) TO hub_runtime;
      CREATE FUNCTION hub.github_app_configuration() RETURNS TABLE(encrypted_value text, version integer)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT s.encrypted_value,s.version FROM public.site_configuration s WHERE s.key='github-app'
      $$;
      REVOKE ALL ON FUNCTION hub.github_app_configuration() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.github_app_configuration() TO hub_runtime,hub_indexer;
      CREATE POLICY project_scope_activation ON projects FOR UPDATE TO hub_owner
        USING(hub.has_project_cap(id,'repositories:manage')) WITH CHECK(hub.can_access_project(id));
      ALTER TABLE tasks ADD COLUMN repository_id uuid;
      ALTER TABLE tasks ADD CONSTRAINT task_repository_project FOREIGN KEY(repository_id,project_id)
        REFERENCES project_repositories(id,project_id);
      CREATE INDEX ix_tasks_repository ON tasks(project_id,repository_id,created_at);
      CREATE POLICY task_repository_scope ON tasks AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_scope(project_id,repository_id))
        WITH CHECK(hub.can_read_scope(project_id,repository_id));
      ALTER TABLE project_progress ADD COLUMN repository_id uuid;
      ALTER TABLE project_progress ADD CONSTRAINT progress_repository_project FOREIGN KEY(repository_id,project_id)
        REFERENCES project_repositories(id,project_id);
      ALTER TABLE audit_events ADD COLUMN repository_id uuid REFERENCES project_repositories(id);
      CREATE POLICY progress_repository_scope ON project_progress AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_scope(project_id,repository_id))
        WITH CHECK(hub.can_read_scope(project_id,repository_id));
      CREATE POLICY audit_repository_scope ON audit_events AS RESTRICTIVE FOR SELECT TO hub_runtime
        USING(project_id IS NULL OR hub.can_read_scope(project_id,repository_id));

      -- Legacy source IDs equal project IDs, so the old HTTP routes remain stable.
      ALTER TABLE document_repositories ADD COLUMN id uuid;
      UPDATE document_repositories SET id=project_id;
      ALTER TABLE document_repositories ALTER COLUMN id SET NOT NULL;
      ALTER TABLE document_repositories ADD COLUMN repository_id uuid;
      ALTER TABLE document_repositories ADD CONSTRAINT document_repository_project
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id);
      ALTER TABLE document_repositories DROP CONSTRAINT document_repositories_pkey;
      ALTER TABLE document_repositories ADD PRIMARY KEY(id);
      ALTER TABLE document_repositories ADD UNIQUE(id,project_id);
      ALTER TABLE document_repositories ADD CHECK(id=coalesce(repository_id,project_id));
      CREATE INDEX ix_document_repositories_project_id ON document_repositories(project_id);
      ALTER TABLE document_syncs ADD COLUMN source_id uuid;
      UPDATE document_syncs SET source_id=project_id;
      ALTER TABLE document_syncs ALTER COLUMN source_id SET NOT NULL;
      ALTER TABLE document_syncs ADD FOREIGN KEY(source_id,project_id) REFERENCES document_repositories(id,project_id);
      DROP INDEX one_document_sync_active;
      CREATE UNIQUE INDEX one_document_sync_active ON document_syncs(source_id)
        WHERE status IN ('queued','running');
      ALTER TABLE project_documents ADD COLUMN source_id uuid;
      UPDATE project_documents SET source_id=project_id;
      ALTER TABLE project_documents ALTER COLUMN source_id SET NOT NULL;
      ALTER TABLE project_documents DROP CONSTRAINT project_documents_pkey;
      ALTER TABLE project_documents ADD PRIMARY KEY(source_id,path);
      ALTER TABLE project_documents ADD FOREIGN KEY(source_id,project_id) REFERENCES document_repositories(id,project_id);
      CREATE POLICY source_scope ON document_repositories AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_source(project_id,id)) WITH CHECK(hub.can_read_source(project_id,id));
      CREATE POLICY document_scope ON project_documents AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_source(project_id,source_id)) WITH CHECK(hub.can_read_source(project_id,source_id));
      CREATE POLICY sync_scope ON document_syncs AS RESTRICTIVE TO hub_runtime
        USING(hub.can_read_source(project_id,source_id)) WITH CHECK(hub.can_read_source(project_id,source_id));

      CREATE TABLE repository_tickets (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        session_id uuid NOT NULL REFERENCES auth_sessions(id), user_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), digest varchar(64) NOT NULL UNIQUE,
        action varchar(16) NOT NULL CHECK(action IN ('read','write')),
        run_id uuid, lease_epoch integer, expected_commit varchar(64), expires_at timestamptz NOT NULL,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE INDEX ix_repository_tickets_expires_at ON repository_tickets(expires_at);
      GRANT SELECT,INSERT,DELETE ON repository_tickets TO hub_runtime;
      ALTER TABLE repository_tickets ENABLE ROW LEVEL SECURITY;
      ALTER TABLE repository_tickets FORCE ROW LEVEL SECURITY;
      CREATE POLICY ticket_owner ON repository_tickets TO hub_owner USING(true) WITH CHECK(true);
      -- Lookup only discloses an unguessable digest to the API; authorization is checked on every Git leg.
      CREATE POLICY ticket_lookup ON repository_tickets FOR SELECT TO hub_runtime USING(true);
      CREATE POLICY ticket_insert ON repository_tickets FOR INSERT TO hub_runtime WITH CHECK(
        user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND
        device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND
        hub.has_repository_cap(project_id,repository_id,CASE WHEN action='read' THEN 'code:read' ELSE 'code:write' END));
      CREATE POLICY ticket_expiry ON repository_tickets FOR DELETE TO hub_runtime USING(expires_at<now());;

CREATE POLICY inherited_repository_scope ON task_runs AS RESTRICTIVE TO hub_runtime
            USING(EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_runs.task_id AND t.project_id=task_runs.project_id))
            WITH CHECK(EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_runs.task_id AND t.project_id=task_runs.project_id));;

CREATE POLICY inherited_repository_scope ON worklogs AS RESTRICTIVE TO hub_runtime
            USING(EXISTS(SELECT 1 FROM tasks t WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id))
            WITH CHECK(EXISTS(SELECT 1 FROM tasks t WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id));;

CREATE POLICY inherited_repository_scope ON task_decisions AS RESTRICTIVE TO hub_runtime
            USING(EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_decisions.task_id AND t.project_id=task_decisions.project_id))
            WITH CHECK(EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_decisions.task_id AND t.project_id=task_decisions.project_id));;

CREATE POLICY inherited_repository_scope ON work_contexts AS RESTRICTIVE TO hub_runtime
            USING(EXISTS(SELECT 1 FROM tasks t WHERE t.id=work_contexts.task_id AND t.project_id=work_contexts.project_id))
            WITH CHECK(EXISTS(SELECT 1 FROM tasks t WHERE t.id=work_contexts.task_id AND t.project_id=work_contexts.project_id));;

UPDATE alembic_version SET version_num='0009' WHERE alembic_version.version_num = '0008';

-- Running upgrade 0009 -> 0010

CREATE TABLE work_snapshots (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL REFERENCES tasks(id),
        run_id uuid NOT NULL REFERENCES task_runs(id), worklog_id uuid NOT NULL UNIQUE REFERENCES worklogs(id),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        evidence_hash varchar(64) NOT NULL, manifest_hash varchar(64) NOT NULL,
        archive_hash varchar(64) NOT NULL, storage_bytes integer NOT NULL CHECK(storage_bytes>0),
        receipt_hash varchar(64) NOT NULL, metadata jsonb NOT NULL
      );
      CREATE INDEX snapshots_task ON work_snapshots(project_id,task_id,created_at);
      CREATE TABLE code_deliveries (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL REFERENCES project_repositories(id),
        task_id uuid NOT NULL REFERENCES tasks(id), run_id uuid NOT NULL REFERENCES task_runs(id),
        worklog_id uuid NOT NULL REFERENCES worklogs(id), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), git_commit varchar(64) NOT NULL,
        git_ref varchar(200) NOT NULL, receipt_hash varchar(64) NOT NULL,
        UNIQUE(worklog_id,git_commit)
      );
      CREATE INDEX deliveries_task ON code_deliveries(project_id,task_id,created_at);
      CREATE FUNCTION hub.work_archive_usage(pid uuid) RETURNS TABLE(project_bytes bigint,total_bytes bigint)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT coalesce(sum(storage_bytes) FILTER(WHERE project_id=pid),0)::bigint,
          coalesce(sum(storage_bytes),0)::bigint FROM public.work_snapshots
          HAVING hub.can_access_project(pid)
      $$;
      REVOKE ALL ON FUNCTION hub.work_archive_usage(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.work_archive_usage(uuid) TO hub_runtime;;

ALTER TABLE work_snapshots ENABLE ROW LEVEL SECURITY;
          ALTER TABLE work_snapshots FORCE ROW LEVEL SECURITY;
          GRANT SELECT,INSERT ON work_snapshots TO hub_runtime;
          CREATE POLICY evidence_owner ON work_snapshots TO hub_owner USING(true) WITH CHECK(true);
          CREATE POLICY evidence_read ON work_snapshots FOR SELECT TO hub_runtime USING(
            EXISTS(SELECT 1 FROM worklogs w WHERE w.id=work_snapshots.worklog_id
              AND w.project_id=work_snapshots.project_id AND w.task_id=work_snapshots.task_id));
          CREATE POLICY evidence_insert ON work_snapshots FOR INSERT TO hub_runtime WITH CHECK(
            hub.has_project_cap(project_id,'tasks:write') AND
            author_id=nullif(current_setting('hub.user_id',true),'')::uuid AND
            device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND
            EXISTS(SELECT 1 FROM worklogs w WHERE w.id=work_snapshots.worklog_id
              AND w.project_id=work_snapshots.project_id AND w.task_id=work_snapshots.task_id
              AND w.run_id=work_snapshots.run_id AND w.author_id=work_snapshots.author_id
              AND w.device_id=work_snapshots.device_id));;

ALTER TABLE code_deliveries ENABLE ROW LEVEL SECURITY;
          ALTER TABLE code_deliveries FORCE ROW LEVEL SECURITY;
          GRANT SELECT,INSERT ON code_deliveries TO hub_runtime;
          CREATE POLICY evidence_owner ON code_deliveries TO hub_owner USING(true) WITH CHECK(true);
          CREATE POLICY evidence_read ON code_deliveries FOR SELECT TO hub_runtime USING(
            EXISTS(SELECT 1 FROM worklogs w WHERE w.id=code_deliveries.worklog_id
              AND w.project_id=code_deliveries.project_id AND w.task_id=code_deliveries.task_id));
          CREATE POLICY evidence_insert ON code_deliveries FOR INSERT TO hub_runtime WITH CHECK(
            hub.has_project_cap(project_id,'tasks:write') AND
            author_id=nullif(current_setting('hub.user_id',true),'')::uuid AND
            device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND
            EXISTS(SELECT 1 FROM worklogs w WHERE w.id=code_deliveries.worklog_id
              AND w.project_id=code_deliveries.project_id AND w.task_id=code_deliveries.task_id
              AND w.run_id=code_deliveries.run_id AND w.author_id=code_deliveries.author_id
              AND w.device_id=code_deliveries.device_id));;

UPDATE alembic_version SET version_num='0010' WHERE alembic_version.version_num = '0009';

-- Running upgrade 0010 -> 0011

CREATE TABLE closeout_jobs (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), user_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), reason varchar(40) NOT NULL,
        scope jsonb NOT NULL, token_digest varchar(64) NOT NULL, expires_at timestamptz NOT NULL,
        state varchar(24) NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','uploaded','conflicts','complete')),
        seal_id uuid, cleanup_report jsonb
      );
      CREATE INDEX ix_closeout_jobs_project_id ON closeout_jobs(project_id);
      CREATE INDEX ix_closeout_jobs_device_id ON closeout_jobs(device_id,created_at);
      CREATE TABLE closeout_runs (
        job_id uuid NOT NULL REFERENCES closeout_jobs(id), run_id uuid NOT NULL REFERENCES task_runs(id),
        task_id uuid NOT NULL REFERENCES tasks(id), repository_id uuid REFERENCES project_repositories(id),
        lease_epoch integer NOT NULL, context_id uuid, payload jsonb NOT NULL,
        PRIMARY KEY(job_id,run_id)
      );
      CREATE TABLE closeout_seals (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        job_id uuid NOT NULL REFERENCES closeout_jobs(id), inventory jsonb NOT NULL, receipt_hash varchar(64) NOT NULL
      );
      CREATE INDEX ix_closeout_seals_job_id ON closeout_seals(job_id);
      ALTER TABLE closeout_jobs ADD FOREIGN KEY(seal_id) REFERENCES closeout_seals(id);
      ALTER TABLE worklogs ADD COLUMN closeout_id uuid REFERENCES closeout_jobs(id);
      CREATE INDEX ix_worklogs_closeout ON worklogs(closeout_id) WHERE closeout_id IS NOT NULL;
      GRANT SELECT,INSERT ON closeout_jobs,closeout_seals TO hub_runtime;
      GRANT UPDATE(state,seal_id,cleanup_report,expires_at) ON closeout_jobs TO hub_runtime;
      GRANT SELECT ON closeout_runs TO hub_runtime;;

ALTER TABLE closeout_jobs ENABLE ROW LEVEL SECURITY;
          ALTER TABLE closeout_jobs FORCE ROW LEVEL SECURITY;
          CREATE POLICY closeout_owner ON closeout_jobs TO hub_owner USING(true) WITH CHECK(true);;

ALTER TABLE closeout_runs ENABLE ROW LEVEL SECURITY;
          ALTER TABLE closeout_runs FORCE ROW LEVEL SECURITY;
          CREATE POLICY closeout_owner ON closeout_runs TO hub_owner USING(true) WITH CHECK(true);;

ALTER TABLE closeout_seals ENABLE ROW LEVEL SECURITY;
          ALTER TABLE closeout_seals FORCE ROW LEVEL SECURITY;
          CREATE POLICY closeout_owner ON closeout_seals TO hub_owner USING(true) WITH CHECK(true);;

CREATE POLICY closeout_owner_read ON tasks FOR SELECT TO hub_owner USING(true);

CREATE POLICY closeout_owner_read ON task_runs FOR SELECT TO hub_owner USING(true);

CREATE POLICY closeout_owner_read ON work_contexts FOR SELECT TO hub_owner USING(true);

CREATE POLICY closeout_owner_release ON tasks FOR UPDATE TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY closeout_owner_release ON task_runs FOR UPDATE TO hub_owner USING(true) WITH CHECK(true);

CREATE FUNCTION hub.closeout_authenticated(cid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.closeout_jobs c WHERE c.id=cid
          AND c.id=nullif(current_setting('hub.closeout_id',true),'')::uuid
          AND c.token_digest=current_setting('hub.closeout_digest',true)
          AND c.device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND c.user_id=nullif(current_setting('hub.user_id',true),'')::uuid)
      $$;
      CREATE FUNCTION hub.closeout_run_allowed(cid uuid,pid uuid,tid uuid,rid uuid,uid uuid,did uuid)
      RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.closeout_authenticated(cid) AND EXISTS(
          SELECT 1 FROM public.closeout_jobs c JOIN public.closeout_runs r ON r.job_id=c.id
          WHERE c.id=cid AND c.project_id=pid AND c.user_id=uid AND c.device_id=did
            AND c.state<>'complete' AND c.expires_at>clock_timestamp() AND r.run_id=rid AND r.task_id=tid)
      $$;
      CREATE FUNCTION hub.closeout_discover(thumb text) RETURNS SETOF public.closeout_jobs
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT c.* FROM public.closeout_jobs c JOIN public.devices d ON d.id=c.device_id
          WHERE d.thumbprint=thumb ORDER BY c.created_at,c.id
      $$;
      CREATE FUNCTION hub.closeout_freeze(cid uuid) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE c public.closeout_jobs;
      BEGIN
        SELECT * INTO c FROM public.closeout_jobs WHERE id=cid;
        IF NOT FOUND OR NOT hub.has_project_cap(c.project_id,'members:manage') THEN
          RAISE EXCEPTION 'permission denied';
        END IF;
        INSERT INTO public.closeout_runs(job_id,run_id,task_id,repository_id,lease_epoch,context_id,payload)
          SELECT c.id,r.id,r.task_id,t.repository_id,r.lease_epoch,w.id,
            coalesce(w.payload,jsonb_build_object('task',jsonb_build_object('scope',t.scope),'policy',
              jsonb_build_object('protected_paths','[]'::jsonb)))
          FROM public.task_runs r JOIN public.tasks t ON t.id=r.task_id AND t.project_id=r.project_id
          LEFT JOIN LATERAL (SELECT x.id,x.payload FROM public.work_contexts x
            WHERE x.run_id=r.id AND x.author_id=c.user_id AND x.device_id=c.device_id
              AND x.acknowledged_at IS NOT NULL AND x.created_at<=c.created_at
            ORDER BY x.created_at DESC,x.id DESC LIMIT 1) w ON true
          WHERE r.project_id=c.project_id AND r.user_id=c.user_id AND r.device_id=c.device_id
            AND r.role='developer' AND r.created_at<=c.created_at
            AND (c.scope->'repository_ids'='null'::jsonb OR
              c.scope->'repository_ids' @> to_jsonb(t.repository_id::text));
        WITH ended AS (
          UPDATE public.task_runs r SET state='released',ended_at=clock_timestamp()
            WHERE r.state='active' AND r.role='developer'
              AND EXISTS(SELECT 1 FROM public.closeout_runs f WHERE f.job_id=c.id AND f.run_id=r.id)
            RETURNING r.id)
          UPDATE public.tasks t SET active_run_id=NULL,lease_until=NULL,status='ready',
            lease_epoch=lease_epoch+1,version=version+1
          WHERE t.status='in_progress' AND t.active_run_id IN (SELECT id FROM ended);
      END $$;
      CREATE FUNCTION hub.closeout_context(cid uuid,rid uuid,wid uuid) RETURNS jsonb
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT w.payload FROM public.work_contexts w JOIN public.closeout_jobs c ON c.id=cid
        JOIN public.closeout_runs r ON r.job_id=c.id AND r.run_id=rid
        WHERE hub.closeout_authenticated(cid) AND w.id=wid AND w.run_id=rid
          AND w.author_id=c.user_id AND w.device_id=c.device_id AND w.created_at<=c.created_at
          AND w.acknowledged_at<=c.created_at
      $$;
      REVOKE ALL ON FUNCTION hub.closeout_authenticated(uuid),hub.closeout_run_allowed(uuid,uuid,uuid,uuid,uuid,uuid),
        hub.closeout_discover(text),hub.closeout_freeze(uuid),hub.closeout_context(uuid,uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.closeout_authenticated(uuid),hub.closeout_run_allowed(uuid,uuid,uuid,uuid,uuid,uuid),
        hub.closeout_discover(text),hub.closeout_freeze(uuid),hub.closeout_context(uuid,uuid,uuid) TO hub_runtime;
      CREATE POLICY closeout_admin_read ON closeout_jobs FOR SELECT TO hub_runtime USING(
        hub.has_project_cap(project_id,'members:manage'));
      CREATE POLICY closeout_device_read ON closeout_jobs FOR SELECT TO hub_runtime USING(hub.closeout_authenticated(id));
      CREATE POLICY closeout_create ON closeout_jobs FOR INSERT TO hub_runtime
        WITH CHECK(hub.has_project_cap(project_id,'members:manage'));
      CREATE POLICY closeout_update ON closeout_jobs FOR UPDATE TO hub_runtime
        USING(hub.closeout_authenticated(id) OR hub.is_superadmin())
        WITH CHECK(hub.closeout_authenticated(id) OR hub.is_superadmin());
      CREATE POLICY closeout_run_read ON closeout_runs FOR SELECT TO hub_runtime
        USING(hub.closeout_authenticated(job_id));
      CREATE POLICY closeout_seal_read ON closeout_seals FOR SELECT TO hub_runtime
        USING(hub.closeout_authenticated(job_id) OR EXISTS(SELECT 1 FROM closeout_jobs c WHERE c.id=job_id));
      CREATE POLICY closeout_seal_insert ON closeout_seals FOR INSERT TO hub_runtime
        WITH CHECK(hub.closeout_authenticated(job_id));
      CREATE POLICY closeout_log_read ON worklogs FOR SELECT TO hub_runtime USING(
        hub.closeout_authenticated(closeout_id));
      CREATE POLICY closeout_log_insert ON worklogs FOR INSERT TO hub_runtime WITH CHECK(
        hub.closeout_run_allowed(closeout_id,project_id,task_id,run_id,author_id,device_id));
      CREATE POLICY closeout_log_scope ON worklogs AS RESTRICTIVE FOR INSERT TO hub_runtime WITH CHECK(
        closeout_id IS NULL OR hub.closeout_run_allowed(closeout_id,project_id,task_id,run_id,author_id,device_id));
      DROP POLICY inherited_repository_scope ON worklogs;
      CREATE POLICY inherited_repository_scope ON worklogs AS RESTRICTIVE TO hub_runtime
        USING(EXISTS(SELECT 1 FROM tasks t WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id)
          OR hub.closeout_authenticated(closeout_id))
        WITH CHECK(EXISTS(SELECT 1 FROM tasks t WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id)
          OR hub.closeout_run_allowed(closeout_id,project_id,task_id,run_id,author_id,device_id));
      CREATE POLICY closeout_snapshot_insert ON work_snapshots FOR INSERT TO hub_runtime WITH CHECK(
        EXISTS(SELECT 1 FROM worklogs w WHERE w.id=worklog_id AND w.project_id=work_snapshots.project_id
          AND w.task_id=work_snapshots.task_id AND w.run_id=work_snapshots.run_id
          AND hub.closeout_run_allowed(w.closeout_id,w.project_id,w.task_id,w.run_id,work_snapshots.author_id,work_snapshots.device_id)));
      CREATE OR REPLACE FUNCTION hub.work_archive_usage(pid uuid) RETURNS TABLE(project_bytes bigint,total_bytes bigint)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT coalesce(sum(storage_bytes) FILTER(WHERE project_id=pid),0)::bigint,
          coalesce(sum(storage_bytes),0)::bigint FROM public.work_snapshots
          HAVING hub.can_access_project(pid) OR EXISTS(SELECT 1 FROM public.closeout_jobs c
            WHERE c.project_id=pid AND hub.closeout_authenticated(c.id) AND c.state<>'complete'
              AND c.expires_at>clock_timestamp())
      $$;;

UPDATE alembic_version SET version_num='0011' WHERE alembic_version.version_num = '0010';

-- Running upgrade 0011 -> 0012

DO $$ BEGIN
        IF NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') THEN
          RAISE EXCEPTION 'Provision the pinned vector extension before migration 0012';
        END IF;
      END $$;
      CREATE TABLE document_versions (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), source_id uuid NOT NULL,
        configuration_version integer NOT NULL, snapshot_id uuid NOT NULL,
        repository_url varchar(400) NOT NULL, branch varchar(200) NOT NULL, git_commit varchar(64),
        path varchar(800) NOT NULL, title varchar(300) NOT NULL, content text NOT NULL,
        sha256 varchar(64) NOT NULL, content_hash varchar(64) NOT NULL,
        original_bytes bytea, exact_bytes boolean NOT NULL DEFAULT false,
        UNIQUE(source_id,configuration_version,path,sha256),
        FOREIGN KEY(source_id,project_id) REFERENCES document_repositories(id,project_id)
      );
      ALTER TABLE project_documents ADD COLUMN version_id uuid REFERENCES document_versions(id);
      CREATE TABLE memory_jobs (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id),
        worklog_id uuid UNIQUE REFERENCES worklogs(id), document_id uuid UNIQUE REFERENCES document_versions(id),
        status varchar(16) NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','succeeded','failed')),
        attempts integer NOT NULL DEFAULT 0, finished_at timestamptz, error_code varchar(64),
        CHECK(num_nonnulls(worklog_id,document_id)=1)
      );
      CREATE INDEX ix_memory_jobs_queue ON memory_jobs(created_at,id) WHERE status='queued';
      CREATE TABLE memory_cards (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id),
        worklog_id uuid REFERENCES worklogs(id), document_id uuid REFERENCES document_versions(id),
        ordinal integer NOT NULL CHECK(ordinal>=0), title varchar(300) NOT NULL,
        body text NOT NULL CHECK(octet_length(body)<=12000), source_hash varchar(64) NOT NULL,
        line_start integer, line_end integer, truncated boolean NOT NULL DEFAULT false,
        extractor varchar(40) NOT NULL,
        CHECK(num_nonnulls(worklog_id,document_id)=1),
        UNIQUE(worklog_id,ordinal), UNIQUE(document_id,ordinal)
      );
      CREATE INDEX ix_memory_cards_project ON memory_cards(project_id,created_at DESC,id);
      CREATE TABLE memory_events (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        card_id uuid NOT NULL REFERENCES memory_cards(id), project_id uuid NOT NULL REFERENCES projects(id),
        version integer NOT NULL CHECK(version>0), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id),
        state varchar(16) NOT NULL CHECK(state IN ('active','conflicted','retired')),
        reason text NOT NULL, UNIQUE(card_id,version)
      );
      -- An empty, rebuildable semantic index. No fabricated or hashed embeddings.
      CREATE TABLE memory_embeddings (
        card_id uuid NOT NULL REFERENCES memory_cards(id), model varchar(200) NOT NULL,
        dimensions integer NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
        content_hash varchar(64) NOT NULL, embedding vector NOT NULL,
        CHECK(vector_dims(embedding)=dimensions), PRIMARY KEY(card_id,model)
      );
      CREATE FUNCTION hub.queue_memory_source() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF TG_TABLE_NAME='worklogs' THEN
          INSERT INTO public.memory_jobs(id,project_id,worklog_id)
            VALUES(gen_random_uuid(),NEW.project_id,NEW.id) ON CONFLICT(worklog_id) DO NOTHING;
        ELSE
          INSERT INTO public.memory_jobs(id,project_id,document_id)
            VALUES(gen_random_uuid(),NEW.project_id,NEW.id) ON CONFLICT(document_id) DO NOTHING;
        END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.queue_memory_source() FROM PUBLIC;
      CREATE TRIGGER memory_log_enqueued AFTER INSERT ON worklogs
        FOR EACH ROW EXECUTE FUNCTION hub.queue_memory_source();
      CREATE TRIGGER memory_document_enqueued AFTER INSERT ON document_versions
        FOR EACH ROW EXECUTE FUNCTION hub.queue_memory_source();
      GRANT USAGE ON SCHEMA public,hub TO hub_memory;
      GRANT SELECT ON document_versions,memory_jobs,memory_cards,memory_events,memory_embeddings TO hub_runtime;
      GRANT INSERT ON memory_events TO hub_runtime;
      GRANT UPDATE(status,attempts,error_code,finished_at) ON memory_jobs TO hub_runtime;
      GRANT SELECT,INSERT ON document_versions TO hub_indexer;
      GRANT SELECT,UPDATE ON memory_jobs TO hub_memory;
      GRANT SELECT,INSERT ON memory_cards TO hub_memory;
      GRANT SELECT ON document_versions,worklogs TO hub_memory;
      GRANT UPDATE(version_id) ON project_documents TO hub_indexer;
      CREATE POLICY memory_worker_logs ON worklogs FOR SELECT TO hub_memory USING(true);;

ALTER TABLE document_versions ENABLE ROW LEVEL SECURITY;

ALTER TABLE document_versions FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_owner ON document_versions TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE memory_jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE memory_jobs FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_owner ON memory_jobs TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE memory_cards ENABLE ROW LEVEL SECURITY;

ALTER TABLE memory_cards FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_owner ON memory_cards TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE memory_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE memory_events FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_owner ON memory_events TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE memory_embeddings ENABLE ROW LEVEL SECURITY;

ALTER TABLE memory_embeddings FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_owner ON memory_embeddings TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY version_indexer ON document_versions TO hub_indexer USING(true) WITH CHECK(true);
      CREATE POLICY version_worker ON document_versions FOR SELECT TO hub_memory USING(true);
      CREATE POLICY job_worker ON memory_jobs TO hub_memory USING(true) WITH CHECK(true);
      CREATE POLICY card_worker ON memory_cards TO hub_memory USING(true) WITH CHECK(true);
      CREATE POLICY version_read ON document_versions FOR SELECT TO hub_runtime USING(
        (hub.can_read_documents(project_id) OR hub.can_manage_documents(project_id))
        AND hub.can_read_source(project_id,source_id)
        AND EXISTS(SELECT 1 FROM document_repositories r WHERE r.id=source_id
          AND r.project_id=document_versions.project_id AND r.version=configuration_version));
      -- Invoker functions deliberately see the caller's original-source RLS.
      CREATE FUNCTION hub.memory_source_visible(wid uuid,did uuid) RETURNS boolean
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
        SELECT CASE WHEN wid IS NOT NULL THEN EXISTS(SELECT 1 FROM public.worklogs w WHERE w.id=wid)
          ELSE EXISTS(SELECT 1 FROM public.document_versions d WHERE d.id=did) END
      $$;
      REVOKE ALL ON FUNCTION hub.memory_source_visible(uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.memory_source_visible(uuid,uuid) TO hub_runtime;
      CREATE POLICY job_read ON memory_jobs FOR SELECT TO hub_runtime
        USING(hub.memory_source_visible(worklog_id,document_id));
      CREATE POLICY job_retry ON memory_jobs FOR UPDATE TO hub_runtime
        USING(status='failed' AND hub.has_project_cap(project_id,'tasks:publish')
          AND hub.memory_source_visible(worklog_id,document_id))
        WITH CHECK(status='queued' AND attempts=0 AND hub.has_project_cap(project_id,'tasks:publish')
          AND hub.memory_source_visible(worklog_id,document_id));
      CREATE POLICY card_read ON memory_cards FOR SELECT TO hub_runtime
        USING(hub.memory_source_visible(worklog_id,document_id));
      CREATE POLICY event_read ON memory_events FOR SELECT TO hub_runtime
        USING(EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=card_id AND c.project_id=memory_events.project_id));
      CREATE POLICY event_insert ON memory_events FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=card_id AND c.project_id=memory_events.project_id));
      CREATE POLICY embedding_read ON memory_embeddings FOR SELECT TO hub_runtime
        USING(EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=card_id));
      -- Contexts contain derived documents as well as logs. They are personal receipts.
      DROP POLICY contexts_read ON work_contexts;
      CREATE POLICY contexts_read ON work_contexts FOR SELECT TO hub_runtime USING(
        hub.can_access_project(project_id)
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'items','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=(item->>'id')::uuid))
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'recent_unindexed','[]'::jsonb)
                            || coalesce(payload->'recent_work','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM worklogs w WHERE w.id=(item->>'id')::uuid)));
      -- Existing decoded snapshots cannot claim byte-for-byte preservation retroactively.
      INSERT INTO document_versions(id,project_id,source_id,configuration_version,snapshot_id,
        repository_url,branch,git_commit,path,title,content,sha256,content_hash)
        SELECT gen_random_uuid(),d.project_id,d.source_id,r.version,d.snapshot_id,r.repository_url,r.branch,
          r.git_commit,d.path,d.title,d.content,d.sha256,encode(sha256(convert_to(d.content,'UTF8')),'hex')
          FROM project_documents d JOIN document_repositories r ON r.id=d.source_id;
      UPDATE project_documents d SET version_id=v.id FROM document_versions v
        WHERE d.source_id=v.source_id AND d.path=v.path AND d.sha256=v.sha256;
      INSERT INTO memory_jobs(id,project_id,worklog_id)
        SELECT gen_random_uuid(),project_id,id FROM worklogs ON CONFLICT(worklog_id) DO NOTHING;;

UPDATE alembic_version SET version_num='0012' WHERE alembic_version.version_num = '0011';

-- Running upgrade 0012 -> 0013

ALTER TABLE tasks ADD COLUMN execution_role varchar(24) NOT NULL DEFAULT 'developer'
        CHECK(execution_role IN ('project-manager','architect','designer','developer'));
      ALTER TABLE task_runs DROP CONSTRAINT task_runs_role_check;
      ALTER TABLE task_runs ADD CONSTRAINT task_runs_role_check
        CHECK(role IN ('project-manager','architect','designer','developer','reviewer','tester','publisher'));
      DROP INDEX one_active_developer;
      CREATE UNIQUE INDEX one_active_primary_run ON task_runs(task_id) WHERE state='active';
      DROP POLICY runs_insert ON task_runs;
      CREATE POLICY runs_insert ON task_runs FOR INSERT TO hub_runtime WITH CHECK (
        user_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND ((state='active' AND role IN ('project-manager','architect','designer','developer')
          AND hub.has_project_cap(project_id,'tasks:claim') AND hub.has_project_cap(project_id,'tasks:write')
          AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_runs.project_id AND t.execution_role=task_runs.role))
        OR (state='finished' AND (
          (role IN ('project-manager','architect','publisher') AND hub.has_project_cap(project_id,'tasks:publish'))
          OR (role='reviewer' AND hub.has_project_cap(project_id,'tasks:review'))
          OR (role='tester' AND hub.has_project_cap(project_id,'tasks:test'))))));
      -- Preserve revocation capture for every primary role, including observed design work.
      DO $migration$
      DECLARE definition text;
      BEGIN
        SELECT pg_get_functiondef('hub.closeout_freeze(uuid)'::regprocedure) INTO definition;
        IF length(definition)-length(replace(definition,'r.role=''developer''','')) <> 2*length('r.role=''developer''') THEN
          RAISE EXCEPTION 'Unexpected closeout function; verify primary-role capture before migration';
        END IF;
        definition := replace(definition,'r.role=''developer''',
          'r.role IN (''project-manager'',''architect'',''designer'',''developer'')');
        EXECUTE definition;
      END $migration$;;

UPDATE alembic_version SET version_num='0013' WHERE alembic_version.version_num = '0012';

-- Running upgrade 0013 -> 0014

CREATE TABLE task_dependencies (
        project_id uuid NOT NULL REFERENCES projects(id),
        task_id uuid NOT NULL, depends_on_id uuid NOT NULL,
        PRIMARY KEY(project_id,task_id,depends_on_id),
        FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
        FOREIGN KEY(depends_on_id,project_id) REFERENCES tasks(id,project_id),
        CHECK(task_id<>depends_on_id)
      );
      CREATE INDEX ix_task_dependencies_source ON task_dependencies(project_id,depends_on_id);
      ALTER TABLE task_dependencies ENABLE ROW LEVEL SECURITY;
      ALTER TABLE task_dependencies FORCE ROW LEVEL SECURITY;
      GRANT SELECT,INSERT,DELETE ON task_dependencies TO hub_runtime;
      CREATE POLICY dependency_owner ON task_dependencies TO hub_owner USING(true) WITH CHECK(true);
      CREATE POLICY dependency_read ON task_dependencies FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_dependencies.project_id)
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=depends_on_id AND t.project_id=task_dependencies.project_id));
      CREATE POLICY dependency_insert ON task_dependencies FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish')
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_dependencies.project_id
          AND t.lease_epoch=0 AND t.status IN ('draft','ready'))
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=depends_on_id AND t.project_id=task_dependencies.project_id));
      -- Deleting an unseen edge must not silently change a restricted dependency.
      CREATE POLICY dependency_delete ON task_dependencies FOR DELETE TO hub_runtime USING(
        hub.has_project_cap(project_id,'tasks:publish')
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_dependencies.project_id
          AND t.lease_epoch=0 AND t.status IN ('draft','ready'))
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=depends_on_id AND t.project_id=task_dependencies.project_id));

      -- Only return a gate Boolean for a visible task, never hidden IDs/titles/counts.
      CREATE FUNCTION hub.task_dependencies_satisfied(pid uuid, tid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.project_id=pid
          AND hub.can_read_scope(pid,t.repository_id)) AND NOT EXISTS(
          SELECT 1 FROM public.task_dependencies d JOIN public.tasks t
            ON t.id=d.depends_on_id AND t.project_id=d.project_id
          WHERE d.project_id=pid AND d.task_id=tid AND t.status<>'completed')
      $$;
      CREATE FUNCTION hub.task_has_restricted_dependencies(pid uuid, tid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.project_id=pid
          AND hub.can_read_scope(pid,t.repository_id)) AND EXISTS(
          SELECT 1 FROM public.task_dependencies d JOIN public.tasks t
            ON t.id=d.depends_on_id AND t.project_id=d.project_id
          WHERE d.project_id=pid AND d.task_id=tid AND NOT hub.can_read_scope(pid,t.repository_id))
      $$;
      CREATE FUNCTION hub.task_children_completed(pid uuid, tid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.project_id=pid
          AND hub.can_read_scope(pid,t.repository_id)) AND NOT EXISTS(
          SELECT 1 FROM public.tasks t WHERE t.project_id=pid AND t.parent_id=tid AND t.status<>'completed')
      $$;
      CREATE FUNCTION hub.task_dependency_cycle(pid uuid, tid uuid, deps uuid[]) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        WITH RECURSIVE walk(id) AS (
          SELECT unnest(deps)
          UNION
          SELECT next.id FROM walk w CROSS JOIN LATERAL (
            SELECT d.depends_on_id AS id FROM public.task_dependencies d
              WHERE d.project_id=pid AND d.task_id=w.id
            UNION
            SELECT t.id FROM public.tasks t WHERE t.project_id=pid AND t.parent_id=w.id
          ) next
        )
        SELECT hub.has_project_cap(pid,'tasks:publish') AND EXISTS(
          SELECT 1 FROM public.tasks t WHERE t.project_id=pid AND t.id=tid
            AND hub.can_read_scope(pid,t.repository_id)) AND EXISTS(SELECT 1 FROM walk WHERE id=tid)
      $$;
      REVOKE ALL ON FUNCTION hub.task_dependencies_satisfied(uuid,uuid),hub.task_children_completed(uuid,uuid),
        hub.task_has_restricted_dependencies(uuid,uuid),hub.task_dependency_cycle(uuid,uuid,uuid[]) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.task_dependencies_satisfied(uuid,uuid),hub.task_children_completed(uuid,uuid),
        hub.task_has_restricted_dependencies(uuid,uuid),hub.task_dependency_cycle(uuid,uuid,uuid[]) TO hub_runtime;;

UPDATE alembic_version SET version_num='0014' WHERE alembic_version.version_num = '0013';

-- Running upgrade 0014 -> 0015

ALTER TABLE memory_cards ADD CONSTRAINT memory_cards_identity_project UNIQUE(id,project_id);
      CREATE TABLE memory_facts (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), source_card_id uuid NOT NULL,
        resource_id uuid NOT NULL, author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id),
        topic varchar(120) NOT NULL CHECK(topic ~ '^[a-z0-9][a-z0-9._/-]{0,119}$'),
        kind varchar(20) NOT NULL CHECK(kind IN ('constraint','decision','interface','lesson','pitfall')),
        title varchar(200) NOT NULL, body text NOT NULL CHECK(octet_length(body) BETWEEN 10 AND 6000),
        source_quote text NOT NULL CHECK(octet_length(source_quote) BETWEEN 8 AND 4000),
        source_hash varchar(64) NOT NULL, content_hash varchar(64) NOT NULL, generation jsonb NOT NULL,
        status varchar(20) NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending','active','conflicted','retired','rejected','superseded','source_stale')),
        version integer NOT NULL DEFAULT 1 CHECK(version>0), UNIQUE(id,project_id),
        FOREIGN KEY(source_card_id,project_id) REFERENCES memory_cards(id,project_id)
      );
      CREATE INDEX ix_memory_facts_project ON memory_facts(project_id,created_at DESC,id);
      CREATE INDEX ix_memory_facts_source ON memory_facts(source_card_id);
      CREATE UNIQUE INDEX one_active_memory_fact_topic ON memory_facts(project_id,resource_id,topic)
        WHERE status='active';
      CREATE TABLE memory_fact_events (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        fact_id uuid NOT NULL, project_id uuid NOT NULL REFERENCES projects(id),
        version integer NOT NULL CHECK(version>0),
        state varchar(20) NOT NULL
          CHECK(state IN ('pending','active','conflicted','retired','rejected','superseded','source_stale')),
        reason text NOT NULL, author_id uuid REFERENCES users(id), device_id uuid REFERENCES devices(id),
        override_reason text NOT NULL DEFAULT '', automatic boolean NOT NULL DEFAULT false,
        UNIQUE(fact_id,version), FOREIGN KEY(fact_id,project_id) REFERENCES memory_facts(id,project_id),
        CHECK(automatic OR (author_id IS NOT NULL AND device_id IS NOT NULL))
      );
      CREATE FUNCTION hub.memory_card_resource(cid uuid,pid uuid) RETURNS uuid
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
        SELECT coalesce(t.repository_id,d.source_id,c.project_id)
          FROM public.memory_cards c LEFT JOIN public.worklogs w ON w.id=c.worklog_id
          LEFT JOIN public.tasks t ON t.id=w.task_id LEFT JOIN public.document_versions d ON d.id=c.document_id
          WHERE c.id=cid AND c.project_id=pid
      $$;
      CREATE FUNCTION hub.memory_card_current(cid uuid) RETURNS boolean
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.memory_cards c WHERE c.id=cid
          AND coalesce((SELECT e.state FROM public.memory_events e WHERE e.card_id=c.id
                        ORDER BY e.version DESC LIMIT 1),'active')='active'
          AND (c.worklog_id IS NOT NULL OR EXISTS(
            SELECT 1 FROM public.project_documents d JOIN public.document_versions v ON v.id=d.version_id
              JOIN public.document_repositories r ON r.id=v.source_id AND r.version=v.configuration_version
              WHERE d.version_id=c.document_id)))
      $$;
      REVOKE ALL ON FUNCTION hub.memory_card_resource(uuid,uuid),hub.memory_card_current(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.memory_card_resource(uuid,uuid),hub.memory_card_current(uuid) TO hub_runtime;
      -- Definer invalidation checks the committed source as the migration owner.
      -- FORCE RLS otherwise hides current documents even from their table owner.
      -- Invoker helpers keep the runtime caller's original visibility unchanged.
      CREATE POLICY knowledge_owner_read ON project_documents FOR SELECT TO hub_owner USING(true);
      CREATE POLICY knowledge_owner_read ON document_repositories FOR SELECT TO hub_owner USING(true);
      GRANT SELECT,INSERT ON memory_facts,memory_fact_events TO hub_runtime;
      GRANT UPDATE(status,version) ON memory_facts TO hub_runtime;;

ALTER TABLE memory_facts ENABLE ROW LEVEL SECURITY;

ALTER TABLE memory_facts FORCE ROW LEVEL SECURITY;

CREATE POLICY knowledge_owner ON memory_facts TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE memory_fact_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE memory_fact_events FORCE ROW LEVEL SECURITY;

CREATE POLICY knowledge_owner ON memory_fact_events TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY fact_read ON memory_facts FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=source_card_id AND c.project_id=memory_facts.project_id));
      CREATE POLICY fact_propose ON memory_facts FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:write') AND status='pending' AND version=1
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND resource_id=hub.memory_card_resource(source_card_id,project_id)
        AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=source_card_id AND c.project_id=memory_facts.project_id
                   AND c.source_hash=memory_facts.source_hash AND strpos(c.body,memory_facts.source_quote)>0));
      CREATE POLICY fact_review ON memory_facts FOR UPDATE TO hub_runtime
        USING(hub.has_project_cap(project_id,'tasks:publish')
          AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=source_card_id AND c.project_id=memory_facts.project_id))
        WITH CHECK(hub.has_project_cap(project_id,'tasks:publish')
          AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=source_card_id AND c.project_id=memory_facts.project_id));
      CREATE POLICY fact_event_read ON memory_fact_events FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=fact_id AND f.project_id=memory_fact_events.project_id));
      CREATE POLICY fact_event_write ON memory_fact_events FOR INSERT TO hub_runtime WITH CHECK(
        NOT automatic AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=fact_id AND f.project_id=memory_fact_events.project_id
          AND ((memory_fact_events.state='pending' AND memory_fact_events.version=1
                AND f.author_id=memory_fact_events.author_id AND hub.has_project_cap(f.project_id,'tasks:write'))
               OR (memory_fact_events.state<>'pending' AND hub.has_project_cap(f.project_id,'tasks:publish')))));
      CREATE FUNCTION hub.check_memory_fact_event() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE fid uuid;
      BEGIN
        fid := CASE WHEN TG_TABLE_NAME='memory_facts' THEN (to_jsonb(NEW)->>'id')::uuid
                    ELSE (to_jsonb(NEW)->>'fact_id')::uuid END;
        IF EXISTS(SELECT 1 FROM public.memory_facts f WHERE f.id=fid AND (NOT EXISTS(
          SELECT 1 FROM public.memory_fact_events e WHERE e.fact_id=f.id AND e.version=f.version AND e.state=f.status)
          OR EXISTS(SELECT 1 FROM public.memory_fact_events e WHERE e.fact_id=f.id AND e.version>f.version)))
        THEN RAISE EXCEPTION 'Knowledge state has no matching decision'; END IF;
        RETURN NULL;
      END $$;
      REVOKE ALL ON FUNCTION hub.check_memory_fact_event() FROM PUBLIC;
      CREATE CONSTRAINT TRIGGER fact_state_has_event AFTER INSERT OR UPDATE ON memory_facts
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.check_memory_fact_event();
      CREATE CONSTRAINT TRIGGER fact_event_has_state AFTER INSERT ON memory_fact_events
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.check_memory_fact_event();
      CREATE FUNCTION hub.invalidate_memory_facts() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE changed record; new_row jsonb; old_row jsonb;
      BEGIN
        new_row := CASE WHEN TG_OP<>'DELETE' THEN to_jsonb(NEW) ELSE '{}'::jsonb END;
        old_row := CASE WHEN TG_OP<>'INSERT' THEN to_jsonb(OLD) ELSE '{}'::jsonb END;
        FOR changed IN UPDATE public.memory_facts f SET status='source_stale',version=version+1
          WHERE f.status='active' AND EXISTS(SELECT 1 FROM public.memory_cards c WHERE c.id=f.source_card_id
            AND NOT hub.memory_card_current(c.id) AND (
            (TG_TABLE_NAME='memory_events' AND c.id=(new_row->>'card_id')::uuid)
            OR (TG_TABLE_NAME='project_documents' AND c.document_id=(old_row->>'version_id')::uuid)
            OR (TG_TABLE_NAME='document_repositories' AND EXISTS(SELECT 1 FROM public.document_versions d
              WHERE d.id=c.document_id AND d.source_id=(new_row->>'id')::uuid))))
          RETURNING f.id,f.project_id,f.version
        LOOP
          INSERT INTO public.memory_fact_events(id,fact_id,project_id,version,state,reason,automatic)
            VALUES(gen_random_uuid(),changed.id,changed.project_id,changed.version,'source_stale',
                   'Original source changed, retired or conflicted; review a current citation before reactivation.',true);
        END LOOP;
        RETURN NULL;
      END $$;
      REVOKE ALL ON FUNCTION hub.invalidate_memory_facts() FROM PUBLIC;
      CREATE TRIGGER knowledge_card_invalidated AFTER INSERT ON memory_events
        FOR EACH ROW WHEN(NEW.state IN ('retired','conflicted')) EXECUTE FUNCTION hub.invalidate_memory_facts();
      CREATE CONSTRAINT TRIGGER knowledge_document_replaced AFTER UPDATE ON project_documents
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN(OLD.version_id IS DISTINCT FROM NEW.version_id)
        EXECUTE FUNCTION hub.invalidate_memory_facts();
      CREATE CONSTRAINT TRIGGER knowledge_document_removed AFTER DELETE ON project_documents
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.invalidate_memory_facts();
      CREATE TRIGGER knowledge_configuration_changed AFTER UPDATE ON document_repositories
        FOR EACH ROW WHEN(OLD.version IS DISTINCT FROM NEW.version) EXECUTE FUNCTION hub.invalidate_memory_facts();
      DROP POLICY contexts_read ON work_contexts;
      CREATE POLICY contexts_read ON work_contexts FOR SELECT TO hub_runtime USING(
        hub.can_access_project(project_id)
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'items','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=(item->>'id')::uuid))
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'knowledge','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=(item->>'id')::uuid))
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(payload->'memory'->'recent_unindexed','[]'::jsonb)
                            || coalesce(payload->'recent_work','[]'::jsonb)) item
          WHERE NOT EXISTS(SELECT 1 FROM worklogs w WHERE w.id=(item->>'id')::uuid)));;

UPDATE alembic_version SET version_num='0015' WHERE alembic_version.version_num = '0014';

-- Running upgrade 0015 -> 0016

CREATE TABLE knowledge_embeddings (
        fact_id uuid NOT NULL REFERENCES memory_facts(id), model varchar(200) NOT NULL,
        dimensions integer NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
        content_hash varchar(64) NOT NULL, embedding vector NOT NULL,
        CHECK(vector_dims(embedding)=dimensions), PRIMARY KEY(fact_id,model)
      );
      CREATE TABLE embedding_jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), card_id uuid REFERENCES memory_cards(id),
        fact_id uuid REFERENCES memory_facts(id), model varchar(200) NOT NULL,
        content_hash varchar(64) NOT NULL, status varchar(16) NOT NULL DEFAULT 'queued'
          CHECK(status IN ('queued','running','succeeded','failed','skipped')),
        attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0), error_code varchar(80),
        not_before timestamptz NOT NULL DEFAULT now(), lease_token uuid, lease_until timestamptz,
        finished_at timestamptz, CHECK(num_nonnulls(card_id,fact_id)=1),
        UNIQUE(card_id,model), UNIQUE(fact_id,model)
      );
      CREATE INDEX ix_embedding_jobs_queue ON embedding_jobs(status,not_before,created_at);
      GRANT USAGE ON SCHEMA public,hub TO hub_embedding;
      GRANT SELECT ON memory_cards,memory_facts TO hub_embedding;
      CREATE POLICY embedding_worker_read ON memory_cards FOR SELECT TO hub_embedding USING(true);
      CREATE POLICY embedding_worker_read ON memory_facts FOR SELECT TO hub_embedding USING(true);
      GRANT SELECT,INSERT,UPDATE ON memory_embeddings,knowledge_embeddings,embedding_jobs TO hub_embedding;
      GRANT SELECT ON knowledge_embeddings,embedding_jobs TO hub_runtime;
      GRANT UPDATE(status,attempts,error_code,not_before,finished_at) ON embedding_jobs TO hub_runtime;
      CREATE POLICY embedding_worker ON memory_embeddings TO hub_embedding USING(true) WITH CHECK(true);;

ALTER TABLE knowledge_embeddings ENABLE ROW LEVEL SECURITY;

ALTER TABLE knowledge_embeddings FORCE ROW LEVEL SECURITY;

CREATE POLICY embedding_owner ON knowledge_embeddings TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY embedding_worker ON knowledge_embeddings TO hub_embedding USING(true) WITH CHECK(true);

ALTER TABLE embedding_jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE embedding_jobs FORCE ROW LEVEL SECURITY;

CREATE POLICY embedding_owner ON embedding_jobs TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY embedding_worker ON embedding_jobs TO hub_embedding USING(true) WITH CHECK(true);

CREATE POLICY knowledge_embedding_read ON knowledge_embeddings FOR SELECT TO hub_runtime
        USING(EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=fact_id));
      CREATE POLICY embedding_job_read ON embedding_jobs FOR SELECT TO hub_runtime USING(
        (card_id IS NOT NULL AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=card_id AND c.project_id=embedding_jobs.project_id))
        OR (fact_id IS NOT NULL AND EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=fact_id AND f.project_id=embedding_jobs.project_id)));
      CREATE POLICY embedding_job_retry ON embedding_jobs FOR UPDATE TO hub_runtime
        USING(status='failed' AND hub.has_project_cap(project_id,'tasks:publish'))
        WITH CHECK(status='queued' AND attempts=0 AND hub.has_project_cap(project_id,'tasks:publish'));;

CREATE FUNCTION hub.queue_embedding() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF TG_TABLE_NAME='memory_cards' THEN
          INSERT INTO public.embedding_jobs(project_id,card_id,model,content_hash)
            VALUES(NEW.project_id,NEW.id,'bge-small-zh-v1.5-fp32-75c43b0-cls-mean384s320-v1',encode(sha256(convert_to(NEW.title||E'\n'||NEW.body,'UTF8')),'hex'))
            ON CONFLICT(card_id,model) DO NOTHING;
        ELSIF NEW.status='active' THEN
          INSERT INTO public.embedding_jobs(project_id,fact_id,model,content_hash)
            VALUES(NEW.project_id,NEW.id,'bge-small-zh-v1.5-fp32-75c43b0-cls-mean384s320-v1',encode(sha256(convert_to(NEW.topic||E'\n'||NEW.title||E'\n'||NEW.body,'UTF8')),'hex'))
            ON CONFLICT(fact_id,model) DO UPDATE SET status='queued',attempts=0,error_code=NULL,not_before=now(),
              lease_token=NULL,lease_until=NULL,finished_at=NULL WHERE embedding_jobs.status IN ('failed','skipped');
        END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.queue_embedding() FROM PUBLIC;
      CREATE TRIGGER card_embedding_queued AFTER INSERT ON memory_cards FOR EACH ROW EXECUTE FUNCTION hub.queue_embedding();
      CREATE TRIGGER knowledge_embedding_queued AFTER INSERT OR UPDATE OF status ON memory_facts
        FOR EACH ROW EXECUTE FUNCTION hub.queue_embedding();
      INSERT INTO embedding_jobs(project_id,card_id,model,content_hash)
        SELECT project_id,id,'bge-small-zh-v1.5-fp32-75c43b0-cls-mean384s320-v1',encode(sha256(convert_to(title||E'\n'||body,'UTF8')),'hex') FROM memory_cards;
      INSERT INTO embedding_jobs(project_id,fact_id,model,content_hash)
        SELECT project_id,id,'bge-small-zh-v1.5-fp32-75c43b0-cls-mean384s320-v1',encode(sha256(convert_to(topic||E'\n'||title||E'\n'||body,'UTF8')),'hex')
          FROM memory_facts WHERE status='active';;

UPDATE alembic_version SET version_num='0016' WHERE alembic_version.version_num = '0015';

-- Running upgrade 0016 -> 0017

ALTER TABLE worklogs ADD COLUMN turn_id uuid;
      CREATE INDEX ix_worklogs_run_turn ON worklogs(run_id,turn_id) WHERE turn_id IS NOT NULL;
      CREATE TABLE agent_endpoints (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid,
        user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        grant_id uuid REFERENCES access_grants(id), user_epoch integer NOT NULL, device_epoch integer NOT NULL,
        instance_id uuid NOT NULL, name varchar(100) NOT NULL, harness varchar(24) NOT NULL,
        roles jsonb NOT NULL CHECK(jsonb_typeof(roles)='array' AND jsonb_array_length(roles) BETWEEN 1 AND 4
          AND roles <@ '["project-manager","architect","designer","developer"]'::jsonb),
        enabled boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1 CHECK(version>0),
        last_seen_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(id,project_id), UNIQUE(project_id,device_id,instance_id),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE TABLE agent_dispatches (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL, endpoint_id uuid NOT NULL,
        sender_id uuid NOT NULL REFERENCES users(id), sender_device_id uuid NOT NULL REFERENCES devices(id),
        task_version integer NOT NULL CHECK(task_version>0), endpoint_version integer NOT NULL CHECK(endpoint_version>0),
        note varchar(1000) NOT NULL, state varchar(16) NOT NULL DEFAULT 'queued'
          CHECK(state IN ('queued','received','accepted','declined','cancelled','expired')),
        version integer NOT NULL DEFAULT 1 CHECK(version>0), expires_at timestamptz NOT NULL,
        received_at timestamptz, decided_at timestamptz, reason varchar(1000) NOT NULL DEFAULT '', run_id uuid,
        FOREIGN KEY(endpoint_id,project_id) REFERENCES agent_endpoints(id,project_id),
        FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
        FOREIGN KEY(run_id,task_id,project_id) REFERENCES task_runs(id,task_id,project_id),
        CHECK((state='accepted')=(run_id IS NOT NULL))
      );
      CREATE UNIQUE INDEX uq_agent_dispatch_pending_task ON agent_dispatches(task_id) WHERE state IN ('queued','received');
      CREATE INDEX ix_agent_dispatch_inbox ON agent_dispatches(endpoint_id,created_at,id);
      CREATE INDEX ix_agent_endpoint_project ON agent_endpoints(project_id,repository_id);
      CREATE FUNCTION hub.guard_agent_dispatch_transition() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      BEGIN
        IF OLD.state NOT IN ('queued','received') OR NEW.version<>OLD.version+1
          OR NEW.state NOT IN ('received','accepted','declined','cancelled','expired')
          OR (OLD.state='received' AND NEW.state='received')
          OR (NEW.state IN ('received','accepted','declined') AND NEW.expires_at<=now())
          OR (NEW.state='expired' AND NEW.expires_at>now())
          OR (NEW.state IN ('received','accepted') AND NEW.received_at IS NULL)
          OR (NEW.state<>'received' AND NEW.decided_at IS NULL)
          OR (NEW.state IN ('declined','cancelled') AND length(trim(NEW.reason))=0)
        THEN RAISE EXCEPTION 'invalid dispatch transition' USING ERRCODE='23514'; END IF;
        IF NEW.state='accepted' AND NOT EXISTS(
          SELECT 1 FROM public.task_runs r JOIN public.tasks t ON t.id=r.task_id
          JOIN public.agent_endpoints a ON a.id=NEW.endpoint_id
          WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.project_id=NEW.project_id
            AND r.user_id=a.user_id AND r.device_id=a.device_id AND r.instance_id=a.instance_id AND r.harness=a.harness
            AND r.state='active' AND t.active_run_id=r.id AND t.status='in_progress'
            AND t.lease_until>now())
        THEN RAISE EXCEPTION 'dispatch requires matching active claim' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_agent_dispatch_transition() FROM PUBLIC;
      CREATE TRIGGER agent_dispatch_transition BEFORE UPDATE OF state,version,received_at,decided_at,reason,run_id ON agent_dispatches
        FOR EACH ROW EXECUTE FUNCTION hub.guard_agent_dispatch_transition();
      GRANT SELECT,INSERT ON agent_endpoints,agent_dispatches TO hub_runtime;
      GRANT UPDATE(grant_id,user_epoch,device_epoch,name,roles,enabled,version,last_seen_at) ON agent_endpoints TO hub_runtime;
      GRANT UPDATE(state,version,received_at,decided_at,reason,run_id) ON agent_dispatches TO hub_runtime;;

ALTER TABLE agent_endpoints ENABLE ROW LEVEL SECURITY;

ALTER TABLE agent_endpoints FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_owner ON agent_endpoints TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE agent_dispatches ENABLE ROW LEVEL SECURITY;

ALTER TABLE agent_dispatches FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_owner ON agent_dispatches TO hub_owner USING(true) WITH CHECK(true);

CREATE FUNCTION hub.agent_endpoint_eligible(aid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.agent_endpoints a
          JOIN public.users u ON u.id=a.user_id JOIN public.devices d ON d.id=a.device_id AND d.user_id=u.id
          JOIN public.projects p ON p.id=a.project_id
          WHERE a.id=aid AND a.enabled AND p.status='active'
          AND u.status='active' AND d.status='approved' AND a.user_epoch=u.auth_epoch AND a.device_epoch=d.epoch
          AND (u.email IS NULL OR (u.email_verified_at IS NOT NULL AND d.email_verified_at IS NOT NULL))
          AND hub.can_read_scope(a.project_id,a.repository_id)
          AND ((a.user_id=nullif(current_setting('hub.user_id',true),'')::uuid
                AND a.device_id=nullif(current_setting('hub.device_id',true),'')::uuid)
               OR hub.has_project_cap(a.project_id,'tasks:publish'))
          AND (a.repository_id IS NULL OR EXISTS(SELECT 1 FROM public.project_repositories r
            WHERE r.id=a.repository_id AND r.project_id=a.project_id AND r.status='active'))
          AND ((u.platform_role='superadmin' AND u.email_verified_at IS NOT NULL AND d.email_verified_at IS NOT NULL)
            OR EXISTS(SELECT 1 FROM public.project_memberships m JOIN public.access_grants g
              ON g.project_id=m.project_id AND g.user_id=m.user_id
              WHERE m.project_id=a.project_id AND m.user_id=a.user_id AND m.status='active'
              AND g.id=a.grant_id AND g.device_id=a.device_id AND g.status='active' AND g.expires_at>now()
              AND g.membership_epoch=m.epoch AND g.user_epoch=u.auth_epoch AND g.device_epoch=d.epoch
              AND g.capabilities ?& array['project:read','tasks:claim','tasks:write']
              AND (a.repository_id IS NULL AND p.repository_mode='legacy' OR g.repository_permissions @>
                jsonb_build_array(jsonb_build_object('repository_id',a.repository_id::text,'capabilities',jsonb_build_array('code:read','code:write')))))))
      $$;
      REVOKE ALL ON FUNCTION hub.agent_endpoint_eligible(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.agent_endpoint_eligible(uuid) TO hub_runtime;
      CREATE POLICY agent_endpoint_read ON agent_endpoints FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND (
          user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          OR hub.has_project_cap(project_id,'tasks:publish') AND hub.agent_endpoint_eligible(id)));
      CREATE POLICY agent_endpoint_insert ON agent_endpoints FOR INSERT TO hub_runtime WITH CHECK(
        user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND hub.has_project_cap(project_id,'tasks:claim') AND hub.has_project_cap(project_id,'tasks:write')
        AND hub.can_read_scope(project_id,repository_id) AND (repository_id IS NULL OR hub.has_repository_cap(project_id,repository_id,'code:write')));
      CREATE POLICY agent_endpoint_update ON agent_endpoints FOR UPDATE TO hub_runtime
        USING(user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND hub.can_read_scope(project_id,repository_id) AND hub.has_project_cap(project_id,'tasks:write'))
        WITH CHECK(user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND hub.can_read_scope(project_id,repository_id) AND hub.has_project_cap(project_id,'tasks:write'));
      CREATE POLICY agent_dispatch_read ON agent_dispatches FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=agent_dispatches.project_id)
        AND (hub.has_project_cap(project_id,'tasks:publish') OR EXISTS(SELECT 1 FROM agent_endpoints a WHERE a.id=endpoint_id
          AND a.user_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND a.device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND hub.agent_endpoint_eligible(a.id))));
      CREATE POLICY agent_dispatch_insert ON agent_dispatches FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish') AND sender_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND sender_device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND state='queued' AND version=1
        AND hub.agent_endpoint_eligible(endpoint_id) AND EXISTS(SELECT 1 FROM tasks t JOIN agent_endpoints a
          ON a.project_id=t.project_id AND a.repository_id IS NOT DISTINCT FROM t.repository_id
          WHERE t.id=task_id AND a.id=endpoint_id AND t.project_id=agent_dispatches.project_id));
      CREATE POLICY agent_dispatch_update ON agent_dispatches FOR UPDATE TO hub_runtime USING(
        EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=agent_dispatches.project_id)
        AND (hub.has_project_cap(project_id,'tasks:publish') OR EXISTS(SELECT 1 FROM agent_endpoints a WHERE a.id=endpoint_id
          AND a.user_id=nullif(current_setting('hub.user_id',true),'')::uuid AND a.device_id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND hub.agent_endpoint_eligible(a.id)))) WITH CHECK(
        EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=agent_dispatches.project_id)
        AND (hub.has_project_cap(project_id,'tasks:publish') AND state IN ('cancelled','expired') OR EXISTS(
          SELECT 1 FROM agent_endpoints a WHERE a.id=endpoint_id AND a.user_id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND a.device_id=nullif(current_setting('hub.device_id',true),'')::uuid AND hub.agent_endpoint_eligible(a.id)
          AND state IN ('received','accepted','declined')
          AND (run_id IS NULL OR EXISTS(SELECT 1 FROM task_runs r WHERE r.id=run_id AND r.user_id=a.user_id AND r.device_id=a.device_id)))));;

UPDATE alembic_version SET version_num='0017' WHERE alembic_version.version_num = '0016';

-- Running upgrade 0017 -> 0018

CREATE TABLE account_invitations (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        user_id uuid NOT NULL REFERENCES users(id), project_id uuid REFERENCES projects(id),
        created_by uuid NOT NULL REFERENCES users(id), email varchar(254) NOT NULL,
        expires_at timestamptz NOT NULL, consumed_at timestamptz
      );
      CREATE INDEX ix_account_invitations_user_id ON account_invitations(user_id);
      CREATE TABLE account_challenges (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        user_id uuid NOT NULL REFERENCES users(id), invitation_id uuid REFERENCES account_invitations(id),
        email varchar(254) NOT NULL, purpose varchar(16) NOT NULL CHECK(purpose IN ('reset','invitation')),
        thumbprint varchar(64) NOT NULL, user_epoch integer NOT NULL CHECK(user_epoch>0),
        digest varchar(64) NOT NULL, expires_at timestamptz NOT NULL,
        attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5), consumed_at timestamptz,
        CHECK((purpose='invitation')=(invitation_id IS NOT NULL))
      );
      CREATE INDEX ix_account_challenges_user_id ON account_challenges(user_id);
      GRANT SELECT,INSERT ON account_invitations,account_challenges TO hub_runtime;
      GRANT UPDATE(consumed_at) ON account_invitations TO hub_runtime;
      GRANT UPDATE(consumed_at,attempts) ON account_challenges TO hub_runtime;
      CREATE POLICY account_recovery_project_read ON projects FOR SELECT TO hub_owner USING(true);
      CREATE FUNCTION hub.account_invitation_eligible(iid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.account_invitations i JOIN public.users u ON u.id=i.user_id
          WHERE i.id=iid AND i.consumed_at IS NULL AND i.expires_at>now()
          AND u.status='pending_email' AND u.email=i.email AND (i.project_id IS NULL OR EXISTS(
            SELECT 1 FROM public.projects p JOIN public.project_memberships m ON m.project_id=p.id
            WHERE p.id=i.project_id AND p.status='active' AND m.user_id=u.id AND m.status='active')))
      $$;
      REVOKE ALL ON FUNCTION hub.account_invitation_eligible(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.account_invitation_eligible(uuid) TO hub_runtime;
      CREATE FUNCTION hub.account_closeout_allowed(uid uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.users u JOIN public.devices d ON d.user_id=u.id
          WHERE u.id=uid AND u.id=nullif(current_setting('hub.user_id',true),'')::uuid
          AND d.id=nullif(current_setting('hub.device_id',true),'')::uuid
          AND u.status='active' AND d.status='approved'
          AND (u.email IS NULL OR (u.email_verified_at IS NOT NULL AND d.email_verified_at IS NOT NULL)))
        OR EXISTS(SELECT 1 FROM public.account_challenges c JOIN public.users u ON u.id=c.user_id
          WHERE u.id=uid AND c.id=nullif(current_setting('hub.account_recovery_id',true),'')::uuid
          AND c.digest=current_setting('hub.account_recovery_digest',true)
          AND c.thumbprint=current_setting('hub.account_recovery_thumb',true)
          AND c.consumed_at IS NULL AND c.expires_at>now() AND c.attempts<=5
          AND u.auth_epoch=c.user_epoch AND u.email=c.email AND u.status IN ('active','pending_email'))
      $$;
      CREATE FUNCTION hub.account_closeout_scopes(uid uuid)
      RETURNS TABLE(project_id uuid,user_id uuid,device_id uuid)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT g.project_id,g.user_id,g.device_id FROM public.access_grants g
          WHERE g.user_id=uid AND hub.account_closeout_allowed(uid)
        UNION SELECT r.project_id,r.user_id,r.device_id FROM public.task_runs r
          WHERE r.user_id=uid AND hub.account_closeout_allowed(uid)
      $$;
      REVOKE ALL ON FUNCTION hub.account_closeout_allowed(uuid),hub.account_closeout_scopes(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.account_closeout_allowed(uuid),hub.account_closeout_scopes(uuid)
        TO hub_runtime;
      CREATE POLICY closeout_account_create ON closeout_jobs FOR INSERT TO hub_runtime WITH CHECK(
        hub.account_closeout_allowed(user_id) AND reason IN ('credential_recovery','device_revoked')
        AND EXISTS(SELECT 1 FROM devices d WHERE d.id=device_id AND d.user_id=closeout_jobs.user_id));
      CREATE POLICY closeout_account_return ON closeout_jobs FOR SELECT TO hub_runtime USING(
        hub.account_closeout_allowed(user_id) AND reason IN ('credential_recovery','device_revoked'));
      DO $migration$
      DECLARE definition text; needle text := 'NOT hub.has_project_cap(c.project_id,''members:manage'')';
      BEGIN
        SELECT pg_get_functiondef('hub.closeout_freeze(uuid)'::regprocedure) INTO definition;
        IF length(definition)-length(replace(definition,needle,''))<>length(needle) THEN
          RAISE EXCEPTION 'Unexpected closeout authorization definition';
        END IF;
        definition := replace(definition,needle,
          'NOT (hub.has_project_cap(c.project_id,''members:manage'') OR
            (hub.account_closeout_allowed(c.user_id)
              AND c.reason IN (''credential_recovery'',''device_revoked'')))');
        EXECUTE definition;
      END $migration$;;

UPDATE alembic_version SET version_num='0018' WHERE alembic_version.version_num = '0017';

-- Running upgrade 0018 -> 0019

ALTER TABLE memory_facts ADD COLUMN additional_sources jsonb NOT NULL DEFAULT '[]'
        CHECK(jsonb_typeof(additional_sources)='array' AND jsonb_array_length(additional_sources)<=7);
      CREATE FUNCTION hub.knowledge_sources_current(cid uuid, evidence jsonb) RETURNS boolean
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
        SELECT hub.memory_card_current(cid) AND NOT EXISTS(
          SELECT 1 FROM jsonb_array_elements(evidence) s
          WHERE NOT hub.memory_card_current((s->>'source_card_id')::uuid))
      $$;
      REVOKE ALL ON FUNCTION hub.knowledge_sources_current(uuid,jsonb) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.knowledge_sources_current(uuid,jsonb) TO hub_runtime;
      CREATE POLICY fact_all_sources ON memory_facts AS RESTRICTIVE TO hub_runtime USING(
        NOT EXISTS(SELECT 1 FROM jsonb_array_elements(additional_sources) s
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c
            WHERE c.id=(s->>'source_card_id')::uuid AND c.project_id=memory_facts.project_id)))
        WITH CHECK(NOT EXISTS(SELECT 1 FROM jsonb_array_elements(additional_sources) s
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c
            WHERE c.id=(s->>'source_card_id')::uuid AND c.project_id=memory_facts.project_id
              AND c.source_hash=s->>'source_hash' AND length(s->>'source_quote')>=8
              AND strpos(c.body,s->>'source_quote')>0)));
      DO $migration$
      DECLARE definition text; needle text := 'c.id=f.source_card_id';
      BEGIN
        SELECT pg_get_functiondef('hub.invalidate_memory_facts()'::regprocedure) INTO definition;
        IF length(definition)-length(replace(definition,needle,''))<>length(needle) THEN
          RAISE EXCEPTION 'Unexpected knowledge invalidation definition';
        END IF;
        definition := replace(definition,needle,
          '(c.id=f.source_card_id OR EXISTS(SELECT 1 FROM jsonb_array_elements(f.additional_sources) s
            WHERE c.id=(s->>''source_card_id'')::uuid))');
        EXECUTE definition;
      END $migration$;;

UPDATE alembic_version SET version_num='0019' WHERE alembic_version.version_num = '0018';

-- Running upgrade 0019 -> 0020

CREATE TABLE project_collaboration (
        project_id uuid NOT NULL REFERENCES projects(id), version integer NOT NULL CHECK(version>0),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(project_id,version)
      );
      ALTER TABLE project_collaboration ENABLE ROW LEVEL SECURITY;
      ALTER TABLE project_collaboration FORCE ROW LEVEL SECURITY;
      GRANT SELECT,INSERT ON project_collaboration TO hub_runtime;
      CREATE POLICY collaboration_owner ON project_collaboration TO hub_owner USING(true) WITH CHECK(true);
      CREATE POLICY collaboration_read ON project_collaboration FOR SELECT TO hub_runtime
        USING(hub.can_access_project(project_id));
      CREATE POLICY collaboration_insert ON project_collaboration FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      ALTER TABLE tasks ADD COLUMN role_profile varchar(64);
      ALTER TABLE tasks ADD COLUMN workflow varchar(16) NOT NULL DEFAULT 'standard'
        CHECK(workflow IN ('standard','compact','strict'));
      ALTER TABLE tasks ADD COLUMN workflow_reason varchar(1000) NOT NULL DEFAULT '';
      ALTER TABLE tasks ADD COLUMN workflow_policy jsonb NOT NULL DEFAULT '{}';
      GRANT UPDATE(workflow,workflow_reason,workflow_policy) ON tasks TO hub_runtime;
      ALTER TABLE agent_endpoints ADD COLUMN role_profiles jsonb NOT NULL DEFAULT '[]'
        CHECK(jsonb_typeof(role_profiles)='array' AND jsonb_array_length(role_profiles)<=32);
      GRANT UPDATE(role_profiles) ON agent_endpoints TO hub_runtime;;

UPDATE alembic_version SET version_num='0020' WHERE alembic_version.version_num = '0019';

-- Running upgrade 0020 -> 0021

CREATE TABLE usage_policies (
        project_id uuid NOT NULL REFERENCES projects(id), version integer NOT NULL CHECK(version>0),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(project_id,version)
      );
      CREATE TABLE usage_events (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid, task_id uuid NOT NULL, run_id uuid NOT NULL,
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        reference_hash varchar(64) NOT NULL, source varchar(24) NOT NULL CHECK(source IN ('host-report','context-measurement')),
        provider varchar(100) NOT NULL, model varchar(200) NOT NULL,
        input_tokens bigint CHECK(input_tokens BETWEEN 0 AND 10000000),
        output_tokens bigint CHECK(output_tokens BETWEEN 0 AND 10000000),
        cached_input_tokens bigint CHECK(cached_input_tokens BETWEEN 0 AND input_tokens),
        reported_cost_micro_usd bigint CHECK(reported_cost_micro_usd BETWEEN 0 AND 1000000000),
        context_bytes bigint CHECK(context_bytes BETWEEN 0 AND 1048576),
        UNIQUE(project_id,run_id,source,reference_hash),
        FOREIGN KEY(run_id,task_id,project_id) REFERENCES task_runs(id,task_id,project_id),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        CHECK((source='host-report' AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL
          AND context_bytes IS NULL) OR (source='context-measurement' AND context_bytes IS NOT NULL
          AND input_tokens IS NULL AND output_tokens IS NULL AND reported_cost_micro_usd IS NULL))
      );
      CREATE INDEX ix_usage_project_period ON usage_events(project_id,created_at);
      CREATE TABLE memory_evaluations (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid,
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        payload jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE INDEX ix_memory_evaluations_project ON memory_evaluations(project_id,created_at,id);
      GRANT SELECT,INSERT ON usage_policies,usage_events,memory_evaluations TO hub_runtime;;

ALTER TABLE usage_policies ENABLE ROW LEVEL SECURITY;

ALTER TABLE usage_policies FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON usage_policies TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE usage_events FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON usage_events TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE memory_evaluations ENABLE ROW LEVEL SECURITY;

ALTER TABLE memory_evaluations FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON memory_evaluations TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY usage_policy_read ON usage_policies FOR SELECT TO hub_runtime USING(hub.can_access_project(project_id));
      CREATE POLICY usage_policy_write ON usage_policies FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY usage_read ON usage_events FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND (hub.has_project_cap(project_id,'tasks:publish')
        OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid));
      CREATE POLICY usage_insert ON usage_events FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:write') AND hub.can_read_scope(project_id,repository_id)
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=run_id
          AND r.user_id=usage_events.author_id AND r.device_id=usage_events.device_id
          AND t.repository_id IS NOT DISTINCT FROM usage_events.repository_id));
      CREATE POLICY evaluation_read ON memory_evaluations FOR SELECT TO hub_runtime USING(
        hub.has_project_cap(project_id,'tasks:publish') AND hub.can_access_project(project_id)
        AND (repository_id IS NULL OR hub.can_read_scope(project_id,repository_id))
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(payload->'source_card_ids') s
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=s::uuid AND c.project_id=memory_evaluations.project_id)));
      CREATE POLICY evaluation_insert ON memory_evaluations FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:publish') AND hub.can_access_project(project_id)
        AND (repository_id IS NULL OR hub.can_read_scope(project_id,repository_id))
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(payload->'source_card_ids') s
          WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=s::uuid AND c.project_id=memory_evaluations.project_id)));
      CREATE FUNCTION hub.usage_budget_open(pid uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND COALESCE((
          SELECT (p.content->>'mode')<>'pause-managed-work'
            OR (p.content->>'monthly_reported_token_limit')::bigint=0
            OR (SELECT COALESCE(sum(e.input_tokens+e.output_tokens),0) FROM public.usage_events e
                WHERE e.project_id=pid AND e.source='host-report'
                AND e.created_at>=date_trunc('month',CURRENT_TIMESTAMP AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
              <(p.content->>'monthly_reported_token_limit')::bigint
          FROM public.usage_policies p WHERE p.project_id=pid ORDER BY p.version DESC LIMIT 1),true)
      $$;
      REVOKE ALL ON FUNCTION hub.usage_budget_open(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.usage_budget_open(uuid) TO hub_runtime;;

UPDATE alembic_version SET version_num='0021' WHERE alembic_version.version_num = '0020';

-- Running upgrade 0021 -> 0022

ALTER TABLE tasks ADD COLUMN documentation jsonb NOT NULL DEFAULT '{"kind":"none"}';
      -- The trigger may inspect all frozen contributor records. Runtime log policies stay unchanged.
      CREATE POLICY guide_source_owner ON worklogs FOR SELECT TO hub_owner USING(true);
      CREATE TABLE guide_jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid,
        task_id uuid NOT NULL REFERENCES tasks(id), source_worklog_id uuid NOT NULL UNIQUE REFERENCES worklogs(id),
        source_hash varchar(64) NOT NULL, target_user_id uuid NOT NULL REFERENCES users(id),
        target_device_id uuid NOT NULL REFERENCES devices(id), instance_id uuid NOT NULL,
        harness varchar(24) NOT NULL, plan jsonb NOT NULL,
        state varchar(20) NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','review','approved','published')),
        version integer NOT NULL DEFAULT 1, lease_epoch integer NOT NULL DEFAULT 0, lease_until timestamptz,
        draft_version integer NOT NULL DEFAULT 0, audience varchar(16) NOT NULL DEFAULT 'repository'
          CHECK(audience IN ('repository','project')),
        reason varchar(2000) NOT NULL DEFAULT '', published_at timestamptz,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE INDEX ix_guide_jobs_inbox ON guide_jobs(project_id,target_device_id,created_at,id);
      CREATE TABLE guide_requirements (
        submission_id uuid NOT NULL REFERENCES worklogs(id), job_id uuid NOT NULL REFERENCES guide_jobs(id),
        PRIMARY KEY(submission_id,job_id)
      );
      CREATE TABLE guide_revisions (
        job_id uuid NOT NULL REFERENCES guide_jobs(id), version integer NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        PRIMARY KEY(job_id,version)
      );
      CREATE TABLE guide_events (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), job_id uuid NOT NULL REFERENCES guide_jobs(id),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        kind varchar(24) NOT NULL, draft_version integer NOT NULL, payload jsonb NOT NULL
      );
      CREATE FUNCTION hub.queue_submission_guides() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      BEGIN
        IF NEW.submission_id IS NULL OR NEW.submission_id IS NOT DISTINCT FROM OLD.submission_id
          OR NEW.documentation->>'kind'='none' THEN RETURN NEW; END IF;
        WITH contributors AS (
          SELECT DISTINCT ON (r.user_id,r.device_id,r.instance_id) w.*, r.instance_id, r.harness
          FROM public.worklogs w JOIN public.task_runs r ON r.id=w.run_id
          WHERE w.task_id=NEW.id AND w.closeout_id IS NULL AND w.outcome='checkpoint'
          ORDER BY r.user_id,r.device_id,r.instance_id,w.created_at DESC,w.id
        ) INSERT INTO public.guide_jobs(project_id,repository_id,task_id,source_worklog_id,source_hash,
          target_user_id,target_device_id,instance_id,harness,plan)
          SELECT NEW.project_id,NEW.repository_id,NEW.id,c.id,c.receipt_hash,c.author_id,c.device_id,
            c.instance_id,c.harness,NEW.documentation FROM contributors c ON CONFLICT(source_worklog_id) DO NOTHING;
        INSERT INTO public.guide_requirements(submission_id,job_id)
          SELECT NEW.submission_id,j.id FROM public.guide_jobs j JOIN (
            SELECT DISTINCT ON (r.user_id,r.device_id,r.instance_id) w.id
            FROM public.worklogs w JOIN public.task_runs r ON r.id=w.run_id
            WHERE w.task_id=NEW.id AND w.closeout_id IS NULL AND w.outcome='checkpoint'
            ORDER BY r.user_id,r.device_id,r.instance_id,w.created_at DESC,w.id
          ) c ON c.id=j.source_worklog_id ON CONFLICT DO NOTHING;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.queue_submission_guides() FROM PUBLIC;
      CREATE TRIGGER queue_submission_guides AFTER UPDATE OF submission_id ON tasks
        FOR EACH ROW EXECUTE FUNCTION hub.queue_submission_guides();
      CREATE FUNCTION hub.guide_release_ready(tid uuid,wid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.submission_id=wid
          AND hub.can_read_scope(t.project_id,t.repository_id))
          AND EXISTS(SELECT 1 FROM public.guide_requirements WHERE submission_id=wid)
          AND NOT EXISTS(SELECT 1 FROM public.guide_requirements q JOIN public.guide_jobs j ON j.id=q.job_id
            WHERE q.submission_id=wid AND j.state NOT IN ('approved','published'))
      $$;
      REVOKE ALL ON FUNCTION hub.guide_release_ready(uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.guide_release_ready(uuid,uuid) TO hub_runtime;
      GRANT SELECT ON guide_jobs,guide_requirements,guide_revisions,guide_events TO hub_runtime;
      GRANT INSERT ON guide_revisions,guide_events TO hub_runtime;
      GRANT UPDATE(target_user_id,target_device_id,instance_id,harness,state,version,lease_epoch,lease_until,
        draft_version,audience,reason,published_at) ON guide_jobs TO hub_runtime;;

ALTER TABLE guide_jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE guide_jobs FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON guide_jobs TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE guide_requirements ENABLE ROW LEVEL SECURITY;

ALTER TABLE guide_requirements FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON guide_requirements TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE guide_revisions ENABLE ROW LEVEL SECURITY;

ALTER TABLE guide_revisions FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON guide_revisions TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE guide_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE guide_events FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON guide_events TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY job_read ON guide_jobs FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) OR
        state='published' AND audience='project' AND hub.has_project_cap(project_id,'guides:read'));
      CREATE POLICY job_update ON guide_jobs FOR UPDATE TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND (
          hub.has_project_cap(project_id,'tasks:publish') OR hub.has_project_cap(project_id,'tasks:test')
          OR target_user_id=nullif(current_setting('hub.user_id',true),'')::uuid
            AND target_device_id=nullif(current_setting('hub.device_id',true),'')::uuid
            AND hub.has_project_cap(project_id,'tasks:write')))
        WITH CHECK(hub.can_read_scope(project_id,repository_id));
      CREATE POLICY requirement_read ON guide_requirements FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id));
      CREATE POLICY revision_read ON guide_revisions FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id AND
          (hub.can_read_scope(j.project_id,j.repository_id) OR guide_revisions.version=j.draft_version)));
      CREATE POLICY revision_insert ON guide_revisions FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id AND j.target_user_id=author_id
          AND j.target_device_id=device_id AND j.state='running'
          AND hub.can_read_scope(j.project_id,j.repository_id) AND hub.has_project_cap(j.project_id,'tasks:write')));
      CREATE POLICY event_read ON guide_events FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id AND hub.can_read_scope(j.project_id,j.repository_id)));
      CREATE POLICY event_insert ON guide_events FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=job_id AND hub.can_read_scope(j.project_id,j.repository_id)
          AND (hub.has_project_cap(j.project_id,'tasks:write') OR hub.has_project_cap(j.project_id,'tasks:publish')
            OR hub.has_project_cap(j.project_id,'tasks:test'))));;

ALTER TABLE guide_events ADD CONSTRAINT guide_event_version_required
        CHECK(payload ? 'job_version' AND jsonb_typeof(payload->'job_version')='number'
          AND (payload->>'job_version')::integer>1);
      ALTER TABLE guide_events ADD CONSTRAINT guide_review_fields_required CHECK(kind<>'reviewed' OR
        payload ?& ARRAY['expected_version','draft_version','gate','approved','reason','audience','override_reason']
          AND jsonb_typeof(payload->'approved')='boolean' AND payload->>'gate' IN ('product','test')
          AND length(trim(payload->>'reason'))>=10);
      CREATE UNIQUE INDEX uq_guide_event_version ON guide_events(job_id,((payload->>'job_version')::integer));
      CREATE FUNCTION hub.guard_guide_transition() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE uid uuid:=nullif(current_setting('hub.user_id',true),'')::uuid;
        did uuid:=nullif(current_setting('hub.device_id',true),'')::uuid;
        writer boolean; reviewer boolean; publisher boolean; reassigned boolean;
      BEGIN
        IF current_user='hub_owner' THEN RETURN NEW; END IF;
        writer:=hub.has_project_cap(NEW.project_id,'tasks:write') AND OLD.target_user_id=uid
          AND OLD.target_device_id=did AND (NEW.repository_id IS NULL OR
            hub.has_repository_cap(NEW.project_id,NEW.repository_id,'code:write'));
        publisher:=hub.has_project_cap(NEW.project_id,'tasks:publish');
        reviewer:=publisher OR hub.has_project_cap(NEW.project_id,'tasks:test');
        reassigned:=NEW.lease_epoch=OLD.lease_epoch+1 AND NEW.state='queued' AND publisher;
        IF NEW.version<>OLD.version+1 OR OLD.state='published'
          OR NEW.draft_version<OLD.draft_version OR NEW.draft_version>OLD.draft_version+1
          OR (NEW.target_user_id,NEW.target_device_id,NEW.instance_id,NEW.harness)
              IS DISTINCT FROM (OLD.target_user_id,OLD.target_device_id,OLD.instance_id,OLD.harness) AND NOT reassigned
          OR NEW.audience<>OLD.audience AND NOT (OLD.state='review' AND NEW.state IN ('review','approved') AND publisher)
          OR (NEW.state='published')<>(NEW.published_at IS NOT NULL)
          OR (NEW.state='running')<>(NEW.lease_until IS NOT NULL)
        THEN RAISE EXCEPTION 'invalid guide transition' USING ERRCODE='23514'; END IF;
        IF reassigned THEN
          IF length(trim(NEW.reason))<10 OR NEW.draft_version<>OLD.draft_version OR NOT EXISTS(
            SELECT 1 FROM public.agent_endpoints a WHERE a.project_id=NEW.project_id
              AND a.repository_id IS NOT DISTINCT FROM NEW.repository_id AND a.user_id=NEW.target_user_id
              AND a.device_id=NEW.target_device_id AND a.instance_id=NEW.instance_id AND a.harness=NEW.harness
              AND hub.agent_endpoint_eligible(a.id))
          THEN RAISE EXCEPTION 'guide reassignment requires eligible recipient' USING ERRCODE='23514'; END IF;
        ELSIF NEW.state='running' THEN
          IF NOT writer OR NOT (OLD.state='queued' OR OLD.state='running' AND OLD.lease_until<=now())
            OR NEW.lease_epoch<>OLD.lease_epoch+1 OR NEW.lease_until<=now()
            OR NEW.lease_until>now()+interval '11 minutes' OR NEW.draft_version<>OLD.draft_version
          THEN RAISE EXCEPTION 'invalid guide claim' USING ERRCODE='23514'; END IF;
        ELSIF OLD.state='running' THEN
          IF NOT writer OR OLD.lease_until<=now() OR NEW.lease_epoch<>OLD.lease_epoch
            OR NEW.state NOT IN ('queued','review')
            OR NEW.draft_version<>OLD.draft_version+(CASE WHEN NEW.state='review' THEN 1 ELSE 0 END)
            OR NEW.state='queued' AND length(trim(NEW.reason))<10
          THEN RAISE EXCEPTION 'invalid guide generation' USING ERRCODE='23514'; END IF;
        ELSIF OLD.state='review' THEN
          IF NOT reviewer OR NEW.state NOT IN ('queued','review','approved')
            OR NEW.lease_epoch<>OLD.lease_epoch OR NEW.draft_version<>OLD.draft_version
          THEN RAISE EXCEPTION 'invalid guide review' USING ERRCODE='23514'; END IF;
        ELSIF NOT (OLD.state='approved' AND NEW.state='published' AND publisher
          AND NEW.draft_version=OLD.draft_version AND NEW.lease_epoch=OLD.lease_epoch) THEN
          RAISE EXCEPTION 'invalid guide publication' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_guide_transition() FROM PUBLIC;
      CREATE TRIGGER guide_transition BEFORE UPDATE ON guide_jobs
        FOR EACH ROW EXECUTE FUNCTION hub.guard_guide_transition();
      CREATE FUNCTION hub.check_guide_transition_evidence() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE uid uuid:=nullif(current_setting('hub.user_id',true),'')::uuid;
        did uuid:=nullif(current_setting('hub.device_id',true),'')::uuid;
        e public.guide_events; r public.guide_revisions; expected_kind text;
      BEGIN
        IF uid IS NULL THEN RETURN NEW; END IF;
        expected_kind:=CASE WHEN NEW.state='running' THEN 'claimed'
          WHEN NEW.state='published' THEN 'published'
          WHEN NEW.state='queued' AND NEW.lease_epoch>OLD.lease_epoch THEN 'reassigned'
          WHEN OLD.state='running' AND NEW.state='queued' THEN 'retry'
          WHEN OLD.state='running' AND NEW.state='review' THEN 'submitted' ELSE 'reviewed' END;
        SELECT * INTO e FROM public.guide_events WHERE job_id=NEW.id
          AND (payload->>'job_version')::integer=NEW.version;
        IF NOT FOUND OR e.author_id<>uid OR e.device_id<>did OR e.kind<>expected_kind
          OR e.draft_version<>NEW.draft_version
        THEN RAISE EXCEPTION 'guide transition requires matching immutable event' USING ERRCODE='23514'; END IF;
        SELECT * INTO r FROM public.guide_revisions WHERE job_id=NEW.id AND version=NEW.draft_version;
        IF expected_kind='submitted' AND (r.job_id IS NULL OR r.author_id<>uid OR r.device_id<>did)
        THEN RAISE EXCEPTION 'guide submission requires authored revision' USING ERRCODE='23514'; END IF;
        IF expected_kind='reviewed' THEN
          IF r.job_id IS NULL OR (e.payload->>'expected_version')::integer<>OLD.version
            OR (e.payload->>'draft_version')::integer<>NEW.draft_version
            OR e.payload->>'gate' NOT IN ('product','test')
            OR NOT hub.has_project_cap(NEW.project_id,
              CASE WHEN e.payload->>'gate'='test' THEN 'tasks:test' ELSE 'tasks:publish' END)
            OR r.author_id=uid AND NOT (EXISTS(SELECT 1 FROM public.users WHERE id=uid AND platform_role='superadmin')
              AND length(trim(e.payload->>'override_reason'))>=10)
            OR r.author_id<>uid AND length(coalesce(e.payload->>'override_reason',''))>0
            OR (SELECT count(*) FROM public.guide_events x WHERE x.job_id=NEW.id AND x.kind='reviewed'
              AND x.draft_version=NEW.draft_version AND x.payload->>'gate'=e.payload->>'gate')<>1
            OR (e.payload->>'approved')::boolean AND NEW.state NOT IN ('review','approved')
            OR NOT (e.payload->>'approved')::boolean AND NEW.state<>'queued'
          THEN RAISE EXCEPTION 'invalid independent guide review evidence' USING ERRCODE='23514'; END IF;
        END IF;
        IF NEW.state IN ('approved','published') AND (
          SELECT count(DISTINCT x.payload->>'gate') FROM public.guide_events x WHERE x.job_id=NEW.id
            AND x.kind='reviewed' AND x.draft_version=NEW.draft_version
            AND x.payload->>'gate' IN ('product','test') AND (x.payload->>'approved')::boolean)<>2
        THEN RAISE EXCEPTION 'guide requires product and test approval' USING ERRCODE='23514'; END IF;
        IF NEW.audience<>OLD.audience AND NOT (expected_kind='reviewed' AND e.payload->>'gate'='product'
          AND (e.payload->>'approved')::boolean AND e.payload->>'audience'=NEW.audience)
        THEN RAISE EXCEPTION 'guide audience requires explicit product approval' USING ERRCODE='23514'; END IF;
        IF NEW.state='published' AND NOT EXISTS(SELECT 1 FROM public.tasks t JOIN public.guide_requirements q
          ON q.submission_id=t.submission_id WHERE q.job_id=NEW.id AND t.id=NEW.task_id AND t.status='completed')
        THEN RAISE EXCEPTION 'guide publication requires released submission' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.check_guide_transition_evidence() FROM PUBLIC;
      CREATE CONSTRAINT TRIGGER guide_transition_evidence AFTER UPDATE ON guide_jobs
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.check_guide_transition_evidence();;

UPDATE alembic_version SET version_num='0022' WHERE alembic_version.version_num = '0021';

-- Running upgrade 0022 -> 0023

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
      GRANT SELECT,INSERT ON delivery_policies,delivery_evidence TO hub_runtime;;

ALTER TABLE delivery_policies ENABLE ROW LEVEL SECURITY;

ALTER TABLE delivery_policies FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON delivery_policies TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE delivery_evidence ENABLE ROW LEVEL SECURITY;

ALTER TABLE delivery_evidence FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON delivery_evidence TO hub_owner USING(true) WITH CHECK(true);

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
          AND d.worklog_id=delivery_evidence.worklog_id AND d.git_commit=delivery_evidence.git_commit));;

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
        FOR EACH ROW EXECUTE FUNCTION hub.guard_delivery_release();;

UPDATE alembic_version SET version_num='0023' WHERE alembic_version.version_num = '0022';

-- Running upgrade 0023 -> 0024

GRANT SELECT ON alembic_version TO hub_runtime;

CREATE TABLE distillation_policies (
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        version integer NOT NULL CHECK(version>0), created_at timestamptz NOT NULL DEFAULT now(),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, PRIMARY KEY(repository_id,version),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE TABLE distillation_jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        policy_version integer NOT NULL, source_refs jsonb NOT NULL,
        fingerprint varchar(64) NOT NULL, version integer NOT NULL DEFAULT 1 CHECK(version>0),
        state varchar(20) NOT NULL DEFAULT 'queued'
          CHECK(state IN ('queued','running','succeeded','obsolete')),
        target_user_id uuid REFERENCES users(id), target_device_id uuid REFERENCES devices(id),
        instance_id uuid,
        lease_epoch integer NOT NULL DEFAULT 0, lease_until timestamptz, attempts integer NOT NULL DEFAULT 0,
        fact_ids jsonb NOT NULL DEFAULT '[]', outcome varchar(32), reason varchar(2000) NOT NULL DEFAULT '',
        finished_at timestamptz,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        CHECK(jsonb_typeof(source_refs)='array' AND jsonb_array_length(source_refs) BETWEEN 1 AND 8)
      );
      CREATE TABLE distillation_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        job_id uuid NOT NULL REFERENCES distillation_jobs(id), job_version integer NOT NULL,
        author_id uuid REFERENCES users(id), device_id uuid REFERENCES devices(id),
        state varchar(20) NOT NULL, reason varchar(2000) NOT NULL,
        UNIQUE(job_id,job_version)
      );
      CREATE INDEX ix_distillation_queue ON distillation_jobs(repository_id,state,created_at,id);
      CREATE UNIQUE INDEX ix_distillation_batch_once
        ON distillation_jobs(repository_id,policy_version,fingerprint) WHERE state<>'obsolete';
      GRANT SELECT,INSERT ON distillation_policies TO hub_runtime;
      GRANT SELECT,UPDATE ON distillation_jobs TO hub_runtime;
      GRANT SELECT,INSERT ON distillation_events TO hub_runtime;;

ALTER TABLE distillation_policies ENABLE ROW LEVEL SECURITY;

ALTER TABLE distillation_policies FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON distillation_policies TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE distillation_jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE distillation_jobs FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON distillation_jobs TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE distillation_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE distillation_events FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON distillation_events TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY distillation_policy_read ON distillation_policies FOR SELECT TO hub_runtime
        USING(hub.has_repository_cap(project_id,repository_id,'code:read'));
      CREATE POLICY distillation_policy_insert ON distillation_policies FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_repository_cap(project_id,repository_id,'code:read')
        AND hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY distillation_job_read ON distillation_jobs FOR SELECT TO hub_runtime USING(
        hub.has_repository_cap(project_id,repository_id,'code:read') AND NOT EXISTS(
          SELECT 1 FROM jsonb_array_elements(source_refs) s WHERE NOT EXISTS(
            SELECT 1 FROM memory_cards c WHERE c.id=(s->>'source_card_id')::uuid
              AND c.project_id=distillation_jobs.project_id)));
      CREATE POLICY distillation_job_update ON distillation_jobs FOR UPDATE TO hub_runtime USING(
        hub.has_repository_cap(project_id,repository_id,'code:read')
        AND hub.has_project_cap(project_id,'tasks:write')
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(source_refs) s WHERE NOT EXISTS(
          SELECT 1 FROM memory_cards c WHERE c.id=(s->>'source_card_id')::uuid
            AND c.project_id=distillation_jobs.project_id))) WITH CHECK(
        target_user_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND target_device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY distillation_event_read ON distillation_events FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM distillation_jobs j WHERE j.id=job_id));
      CREATE POLICY distillation_event_insert ON distillation_events FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM distillation_jobs j WHERE j.id=job_id
          AND j.target_user_id=author_id AND j.target_device_id=device_id AND j.version=job_version));

      CREATE FUNCTION hub.enqueue_distillation(rid uuid) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE
        config jsonb; config_version integer; pid uuid; refs jsonb='[]'; source record;
        used_bytes integer=0; size_bytes integer; jid uuid;
      BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('distillation:'||rid::text,0));
        SELECT r.project_id INTO pid FROM project_repositories r JOIN projects p ON p.id=r.project_id
          WHERE r.id=rid AND r.status='active' AND p.status='active';
        IF pid IS NULL THEN RETURN 0; END IF;
        SELECT content,version INTO config,config_version FROM distillation_policies
          WHERE repository_id=rid ORDER BY version DESC LIMIT 1;
        -- Invalidate pending work immediately when its policy or any source is replaced/retired.
        WITH changed AS (
          UPDATE distillation_jobs j SET state='obsolete',version=j.version+1,lease_until=NULL,
            lease_epoch=j.lease_epoch+1,finished_at=now(),
            reason='Policy or source changed; originals retained'
          WHERE j.repository_id=rid AND j.state IN ('queued','running') AND (
            j.policy_version IS DISTINCT FROM config_version
            OR NOT coalesce((config->>'enabled')::boolean,false)
            OR EXISTS(SELECT 1 FROM jsonb_array_elements(j.source_refs) s
              WHERE NOT hub.memory_card_current((s->>'source_card_id')::uuid)))
          RETURNING id,version,state,reason
        ) INSERT INTO distillation_events(job_id,job_version,state,reason)
          SELECT id,version,state,reason FROM changed;
        IF NOT coalesce((config->>'enabled')::boolean,false) THEN RETURN 0; END IF;
        FOR source IN
          SELECT c.*,w.created_at AS original_created_at,d.path AS original_path
            FROM memory_cards c LEFT JOIN worklogs w ON w.id=c.worklog_id
            LEFT JOIN tasks t ON t.id=w.task_id LEFT JOIN document_versions d ON d.id=c.document_id
          WHERE c.project_id=pid AND coalesce(t.repository_id,d.source_id)=rid
            AND hub.memory_card_current(c.id)
            AND NOT EXISTS(SELECT 1 FROM distillation_jobs j WHERE j.repository_id=rid
              AND j.policy_version=config_version AND j.state<>'obsolete'
              AND j.source_refs @> jsonb_build_array(jsonb_build_object('source_card_id',c.id::text)))
          ORDER BY c.created_at,c.id LIMIT (config->>'max_sources')::integer
        LOOP
          size_bytes=octet_length(jsonb_build_object('source_card_id',source.id::text,'source_hash',source.source_hash,
            'title',source.title,'body',source.body,'source_kind',
              CASE WHEN source.worklog_id IS NOT NULL THEN 'worklog' ELSE 'document' END,
            'source_id',coalesce(source.worklog_id,source.document_id)::text,
            'source_path',source.original_path,'source_created_at',source.original_created_at,
            'truncated',source.truncated,'content_trust','untrusted-project-data')::text);
          EXIT WHEN jsonb_array_length(refs)>=(config->>'max_sources')::integer
            OR used_bytes+size_bytes>(config->>'input_budget_bytes')::integer-2048;
          refs=refs||jsonb_build_array(jsonb_build_object('source_card_id',source.id::text,'source_hash',source.source_hash));
          used_bytes=used_bytes+size_bytes;
        END LOOP;
        IF jsonb_array_length(refs)<(config->>'min_sources')::integer THEN RETURN 0; END IF;
        INSERT INTO distillation_jobs(project_id,repository_id,policy_version,source_refs,fingerprint)
          VALUES(pid,rid,config_version,refs,encode(sha256(convert_to(refs::text,'UTF8')),'hex'))
          ON CONFLICT DO NOTHING RETURNING id INTO jid;
        IF jid IS NULL THEN RETURN 0; END IF;
        INSERT INTO distillation_events(job_id,job_version,state,reason)
          VALUES(jid,1,'queued','Automatically batched source references; no model has executed');
        RETURN 1;
      END $$;
      REVOKE ALL ON FUNCTION hub.enqueue_distillation(uuid) FROM PUBLIC;

      CREATE FUNCTION hub.schedule_distillation() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE rid uuid; count_jobs integer=0;
      BEGIN
        FOR rid IN SELECT repository_id FROM (
          SELECT DISTINCT ON (repository_id) repository_id,content FROM distillation_policies
            ORDER BY repository_id,version DESC) current_policy
          WHERE (content->>'enabled')::boolean ORDER BY repository_id LOOP
          count_jobs=count_jobs+hub.enqueue_distillation(rid);
        END LOOP;
        RETURN count_jobs;
      END $$;
      REVOKE ALL ON FUNCTION hub.schedule_distillation() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.schedule_distillation() TO hub_memory;
      CREATE FUNCTION hub.distillation_policy_changed() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ BEGIN
        PERFORM hub.enqueue_distillation(NEW.repository_id); RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.distillation_policy_changed() FROM PUBLIC;
      CREATE TRIGGER distillation_policy_queue AFTER INSERT ON distillation_policies
        FOR EACH ROW EXECUTE FUNCTION hub.distillation_policy_changed();

      CREATE FUNCTION hub.guard_distillation_transition() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$ BEGIN
        IF NEW.id<>OLD.id OR NEW.project_id<>OLD.project_id OR NEW.repository_id<>OLD.repository_id
          OR NEW.source_refs<>OLD.source_refs OR NEW.fingerprint<>OLD.fingerprint
          OR NEW.policy_version<>OLD.policy_version OR NEW.created_at<>OLD.created_at
          OR NEW.version<>OLD.version+1 OR OLD.state IN ('succeeded','obsolete')
          OR (NEW.state='running' AND NOT(OLD.state='queued' OR OLD.lease_until<=now()))
          OR (NEW.state IN ('queued','succeeded') AND OLD.state<>'running') THEN
          RAISE EXCEPTION 'Invalid distillation transition' USING ERRCODE='23514';
        END IF; RETURN NEW;
      END $$;
      CREATE TRIGGER distillation_transition BEFORE UPDATE ON distillation_jobs
        FOR EACH ROW EXECUTE FUNCTION hub.guard_distillation_transition();
      CREATE FUNCTION hub.check_distillation_event() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$ BEGIN
        IF NOT EXISTS(SELECT 1 FROM distillation_events e WHERE e.job_id=NEW.id
          AND e.job_version=NEW.version AND e.state=NEW.state) THEN
          RAISE EXCEPTION 'Distillation event required' USING ERRCODE='23514';
        END IF; RETURN NULL;
      END $$;
      CREATE CONSTRAINT TRIGGER distillation_event_evidence AFTER INSERT OR UPDATE ON distillation_jobs
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hub.check_distillation_event();;

UPDATE alembic_version SET version_num='0024' WHERE alembic_version.version_num = '0023';

-- Running upgrade 0024 -> 0025

CREATE TABLE generation_configuration (
        key varchar(32) PRIMARY KEY CHECK(key='provider'), version integer NOT NULL CHECK(version>0),
        encrypted_value text NOT NULL
      );
      CREATE TABLE generation_policies (
        repository_id uuid NOT NULL, project_id uuid NOT NULL REFERENCES projects(id), version integer NOT NULL CHECK(version>0),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        instance_id uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), content jsonb NOT NULL,
        PRIMARY KEY(repository_id,version),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE TABLE generation_calls (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        policy_version integer NOT NULL, provider_version integer NOT NULL, provider_model varchar(120) NOT NULL,
        kind varchar(20) NOT NULL CHECK(kind IN ('distillation','guide')), job_id uuid NOT NULL,
        state varchar(20) NOT NULL CHECK(state IN ('reserved','calling','ready','shared','uncertain','blocked','abandoned')),
        lease_token uuid NOT NULL, lease_until timestamptz NOT NULL,
        user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id), instance_id uuid NOT NULL,
        job_version integer NOT NULL, job_lease_epoch integer NOT NULL, source_hash varchar(64) NOT NULL,
        reserved_tokens integer NOT NULL CHECK(reserved_tokens>0),
        input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 65536),
        input_tokens bigint CHECK(input_tokens BETWEEN 0 AND 10000000),
        output_tokens bigint CHECK(output_tokens BETWEEN 0 AND 10000000),
        response_hash varchar(64), encrypted_result text, error_code varchar(100), finished_at timestamptz,
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE UNIQUE INDEX uq_generation_job_once ON generation_calls(kind,job_id) WHERE state<>'abandoned';
      CREATE INDEX ix_generation_daily ON generation_calls(created_at);
      CREATE TABLE generation_retry_requests (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        call_id uuid NOT NULL REFERENCES generation_calls(id), user_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), allow_new_provider_call boolean NOT NULL,
        reason varchar(2000) NOT NULL CHECK(length(trim(reason))>=10),
        state varchar(20) NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','applied','rejected')),
        error_code varchar(100)
      );
      CREATE UNIQUE INDEX uq_generation_retry_pending ON generation_retry_requests(call_id) WHERE state='pending';
      GRANT SELECT,INSERT ON generation_retry_requests TO hub_runtime;
      GRANT UPDATE ON generation_retry_requests TO hub_generation;
      GRANT SELECT,INSERT,UPDATE ON generation_configuration TO hub_runtime;
      GRANT SELECT,INSERT ON generation_policies TO hub_runtime;
      GRANT SELECT ON generation_calls TO hub_runtime;
      GRANT SELECT ON generation_configuration,generation_policies TO hub_generation;
      GRANT INSERT,UPDATE ON generation_calls TO hub_generation;
      CREATE TABLE generation_receipts (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid,
        distillation_id uuid REFERENCES distillation_jobs(id), guide_id uuid REFERENCES guide_jobs(id),
        lease_epoch integer NOT NULL CHECK(lease_epoch>0), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), provider varchar(32) NOT NULL CHECK(provider='openai-compatible'),
        model varchar(120) NOT NULL, response_hash varchar(64) NOT NULL CHECK(response_hash~'^[a-f0-9]{64}$'),
        input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 65536),
        input_tokens bigint CHECK(input_tokens BETWEEN 0 AND 10000000),
        output_tokens bigint CHECK(output_tokens BETWEEN 0 AND 10000000),
        CHECK(num_nonnulls(distillation_id,guide_id)=1),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        UNIQUE(distillation_id,lease_epoch), UNIQUE(guide_id,lease_epoch)
      );
      ALTER TABLE generation_receipts ENABLE ROW LEVEL SECURITY;
      ALTER TABLE generation_receipts FORCE ROW LEVEL SECURITY;
      CREATE POLICY owner_access ON generation_receipts TO hub_owner USING(true) WITH CHECK(true);
      GRANT SELECT ON generation_receipts TO hub_runtime;
      GRANT INSERT ON generation_receipts TO hub_generation;
      CREATE POLICY receipt_read ON generation_receipts FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND (
          distillation_id IS NOT NULL AND EXISTS(SELECT 1 FROM distillation_jobs j WHERE j.id=distillation_id)
          OR guide_id IS NOT NULL AND EXISTS(SELECT 1 FROM guide_jobs j WHERE j.id=guide_id)));
      CREATE POLICY receipt_insert ON generation_receipts FOR INSERT TO hub_generation WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND hub.has_project_cap(project_id,'tasks:write') AND hub.can_read_scope(project_id,repository_id)
        AND (distillation_id IS NOT NULL AND EXISTS(SELECT 1 FROM distillation_jobs j
          WHERE j.id=distillation_id AND j.project_id=generation_receipts.project_id
            AND j.repository_id IS NOT DISTINCT FROM generation_receipts.repository_id
            AND j.target_user_id=author_id AND j.target_device_id=device_id
            AND j.lease_epoch=generation_receipts.lease_epoch AND j.state='succeeded')
        OR guide_id IS NOT NULL AND EXISTS(SELECT 1 FROM guide_jobs j
          WHERE j.id=guide_id AND j.project_id=generation_receipts.project_id
            AND j.repository_id IS NOT DISTINCT FROM generation_receipts.repository_id
            AND j.target_user_id=author_id AND j.target_device_id=device_id
            AND j.lease_epoch=generation_receipts.lease_epoch AND j.state='review')));;

ALTER TABLE generation_configuration ENABLE ROW LEVEL SECURITY;

ALTER TABLE generation_configuration FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON generation_configuration TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE generation_policies ENABLE ROW LEVEL SECURITY;

ALTER TABLE generation_policies FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON generation_policies TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE generation_calls ENABLE ROW LEVEL SECURITY;

ALTER TABLE generation_calls FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON generation_calls TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE generation_retry_requests ENABLE ROW LEVEL SECURITY;

ALTER TABLE generation_retry_requests FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON generation_retry_requests TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY configuration_root ON generation_configuration TO hub_runtime
        USING(hub.is_superadmin()) WITH CHECK(hub.is_superadmin());
      CREATE POLICY configuration_worker ON generation_configuration FOR SELECT TO hub_generation USING(true);
      CREATE POLICY generation_policy_read ON generation_policies FOR SELECT TO hub_runtime
        USING(hub.can_read_scope(project_id,repository_id));
      CREATE POLICY generation_policy_insert ON generation_policies FOR INSERT TO hub_runtime WITH CHECK(
        hub.is_superadmin() AND hub.can_read_scope(project_id,repository_id)
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY generation_policy_worker ON generation_policies FOR SELECT TO hub_generation USING(true);
      CREATE POLICY generation_calls_read ON generation_calls FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND hub.has_project_cap(project_id,'tasks:publish'));
      CREATE POLICY generation_calls_worker ON generation_calls TO hub_generation USING(true) WITH CHECK(true);
      CREATE POLICY generation_retry_read ON generation_retry_requests FOR SELECT TO hub_runtime USING(
        hub.is_superadmin());
      CREATE POLICY generation_retry_insert ON generation_retry_requests FOR INSERT TO hub_runtime WITH CHECK(
        hub.is_superadmin() AND state='pending' AND error_code IS NULL
        AND user_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM generation_calls c WHERE c.id=call_id AND c.state IN ('blocked','uncertain')));
      CREATE POLICY generation_retry_worker ON generation_retry_requests TO hub_generation USING(true) WITH CHECK(true);
      CREATE FUNCTION hub.generation_project_budget_open(pid uuid, additional bigint) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT hub.can_access_project(pid) AND additional>=0 AND COALESCE((
          SELECT (p.content->>'mode')<>'pause-managed-work' OR (p.content->>'monthly_reported_token_limit')::bigint=0
            OR (SELECT COALESCE(sum(e.input_tokens+e.output_tokens),0) FROM usage_events e
                WHERE e.project_id=pid AND e.source='host-report' AND e.created_at>=date_trunc('month',now()))
             +(SELECT COALESCE(sum(c.reserved_tokens),0) FROM generation_calls c
                WHERE c.project_id=pid AND c.created_at>=date_trunc('month',now()))+additional
              <=(p.content->>'monthly_reported_token_limit')::bigint
          FROM usage_policies p WHERE p.project_id=pid ORDER BY p.version DESC LIMIT 1),true)
        $$;
      REVOKE ALL ON FUNCTION hub.generation_project_budget_open(uuid,bigint) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.generation_project_budget_open(uuid,bigint) TO hub_runtime;;

UPDATE alembic_version SET version_num='0025' WHERE alembic_version.version_num = '0024';

-- Running upgrade 0025 -> 0026

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
      GRANT SELECT,INSERT ON guide_amendments,guide_amendment_reviews,guide_publications,guide_feedback,guide_feedback_resolutions TO hub_runtime;;

ALTER TABLE guide_amendments ENABLE ROW LEVEL SECURITY;

ALTER TABLE guide_amendments FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON guide_amendments TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE guide_amendment_reviews ENABLE ROW LEVEL SECURITY;

ALTER TABLE guide_amendment_reviews FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON guide_amendment_reviews TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE guide_publications ENABLE ROW LEVEL SECURITY;

ALTER TABLE guide_publications FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON guide_publications TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE guide_feedback ENABLE ROW LEVEL SECURITY;

ALTER TABLE guide_feedback FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON guide_feedback TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE guide_feedback_resolutions ENABLE ROW LEVEL SECURITY;

ALTER TABLE guide_feedback_resolutions FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON guide_feedback_resolutions TO hub_owner USING(true) WITH CHECK(true);

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
      REVOKE ALL ON FUNCTION hub.check_guide_feedback() FROM PUBLIC;;

UPDATE alembic_version SET version_num='0026' WHERE alembic_version.version_num = '0025';

-- Running upgrade 0026 -> 0027

CREATE TABLE code_audit_policies (
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        version integer NOT NULL CHECK(version>0), created_at timestamptz NOT NULL DEFAULT now(),
        author_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        content jsonb NOT NULL, content_hash varchar(64) NOT NULL,
        PRIMARY KEY(repository_id,version),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        CHECK(jsonb_typeof(content->'enabled')='boolean'),
        CHECK(jsonb_typeof(content->'rules')='array' AND jsonb_array_length(content->'rules')<=60),
        CHECK(content->'blocking_severities' @> '["critical","high"]'::jsonb),
        CHECK(length(content->>'change_reason') BETWEEN 10 AND 2000),
        CHECK(NOT (content->>'enabled')::boolean OR jsonb_array_length(content->'rules')>0)
      );
      CREATE TABLE code_audits (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), repository_id uuid NOT NULL,
        task_id uuid NOT NULL REFERENCES tasks(id), worklog_id uuid NOT NULL REFERENCES worklogs(id),
        delivery_id uuid NOT NULL REFERENCES code_deliveries(id),
        policy_version integer NOT NULL, repository_version integer NOT NULL,
        delivery_policy_version integer NOT NULL, project_policy_version integer NOT NULL,
        git_commit varchar(64) NOT NULL, source_hash varchar(64) NOT NULL UNIQUE, task_context jsonb NOT NULL,
        state varchar(16) NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed')),
        version integer NOT NULL DEFAULT 1 CHECK(version>0), lease_epoch integer NOT NULL DEFAULT 0,
        lease_until timestamptz, reviewer_id uuid REFERENCES users(id), device_id uuid REFERENCES devices(id),
        instance_id uuid, override_reason varchar(2000) NOT NULL DEFAULT '', reason varchar(2000) NOT
        NULL DEFAULT '',
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id),
        FOREIGN KEY(repository_id,policy_version) REFERENCES code_audit_policies(repository_id,version),
        CHECK((state='running')=(lease_until IS NOT NULL))
      );
      CREATE INDEX ix_code_audits_inbox ON code_audits(project_id,repository_id,state,created_at DESC,id);
      CREATE INDEX ix_code_audits_task ON code_audits(task_id,worklog_id,created_at DESC,id);
      CREATE TABLE code_audit_reports (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), job_id uuid NOT NULL
        REFERENCES code_audits(id),
        lease_epoch integer NOT NULL CHECK(lease_epoch>0), author_id uuid NOT NULL REFERENCES users(id),
        device_id uuid NOT NULL REFERENCES devices(id), instance_id uuid NOT NULL,
        source_hash varchar(64) NOT NULL, content jsonb NOT NULL, passed boolean NOT NULL,
        receipt_hash varchar(64) NOT NULL, UNIQUE(job_id,lease_epoch)
      );
      CREATE TABLE code_audit_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        job_id uuid NOT NULL REFERENCES code_audits(id), author_id uuid REFERENCES users(id),
        device_id uuid REFERENCES devices(id), kind varchar(24) NOT NULL, version integer NOT NULL,
        payload jsonb NOT NULL, UNIQUE(job_id,version)
      );
      GRANT SELECT,INSERT ON code_audit_policies,code_audit_reports TO hub_runtime;
      GRANT SELECT ON code_audits,code_audit_events TO hub_runtime;
      GRANT UPDATE(state,version,lease_epoch,lease_until,reviewer_id,device_id,instance_id,
        override_reason,reason)
        ON code_audits TO hub_runtime;;

ALTER TABLE code_audit_policies ENABLE ROW LEVEL SECURITY;

ALTER TABLE code_audit_policies FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON code_audit_policies TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE code_audits ENABLE ROW LEVEL SECURITY;

ALTER TABLE code_audits FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON code_audits TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE code_audit_reports ENABLE ROW LEVEL SECURITY;

ALTER TABLE code_audit_reports FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON code_audit_reports TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE code_audit_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE code_audit_events FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON code_audit_events TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY code_audit_progress_owner ON project_progress FOR INSERT TO hub_owner WITH CHECK(true);
      CREATE POLICY audit_policy_read ON code_audit_policies FOR SELECT TO hub_runtime USING(
        hub.has_repository_cap(project_id,repository_id,'code:read'));
      CREATE POLICY audit_policy_insert ON code_audit_policies FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_repository_cap(project_id,repository_id,'code:read') AND
        hub.has_project_cap(project_id,'tasks:publish')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY code_audit_read ON code_audits FOR SELECT TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id)
        AND EXISTS(SELECT 1 FROM worklogs w WHERE w.id=worklog_id AND w.task_id=code_audits.task_id));
      CREATE POLICY code_audit_update ON code_audits FOR UPDATE TO hub_runtime USING(
        hub.can_read_scope(project_id,repository_id) AND hub.has_project_cap(project_id,'tasks:review'))
        WITH CHECK(hub.can_read_scope(project_id,repository_id));
      CREATE POLICY code_report_read ON code_audit_reports FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM code_audits j WHERE j.id=job_id));
      CREATE POLICY code_report_insert ON code_audit_reports FOR INSERT TO hub_runtime WITH CHECK(
        author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM code_audits j WHERE j.id=job_id AND j.state='running'
          AND j.reviewer_id=author_id AND j.device_id=code_audit_reports.device_id
          AND j.instance_id=code_audit_reports.instance_id AND j.lease_epoch=code_audit_reports.lease_epoch
          AND j.lease_until>now() AND hub.has_project_cap(j.project_id,'tasks:review')
          AND hub.has_project_cap(j.project_id,'logs:read')));
      CREATE POLICY code_event_read ON code_audit_events FOR SELECT TO hub_runtime USING(
        EXISTS(SELECT 1 FROM code_audits j WHERE j.id=job_id));

      CREATE FUNCTION hub.code_audit_task_context(tid uuid) RETURNS jsonb
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT jsonb_build_object('id',t.id,'title',t.title,'description',t.description,
          'acceptance',t.acceptance,'scope',t.scope,'execution_role',t.execution_role)
          FROM public.tasks t WHERE t.id=tid
      $$;
      REVOKE ALL ON FUNCTION hub.code_audit_task_context(uuid) FROM PUBLIC;

      CREATE FUNCTION hub.enqueue_code_audit(did uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE d code_deliveries%ROWTYPE; p code_audit_policies%ROWTYPE;
        rv integer; dv integer; pv integer; ctx jsonb; fingerprint text; jid uuid;
      BEGIN
        SELECT * INTO d FROM public.code_deliveries WHERE id=did;
        IF d.id IS NULL THEN RETURN NULL; END IF;
        SELECT * INTO p FROM public.code_audit_policies WHERE repository_id=d.repository_id
          ORDER BY version DESC LIMIT 1;
        IF p.repository_id IS NULL OR NOT (p.content->>'enabled')::boolean THEN RETURN NULL; END IF;
        SELECT version INTO rv FROM public.project_repositories WHERE id=d.repository_id;
        SELECT coalesce(max(version),0) INTO dv FROM public.delivery_policies WHERE
        repository_id=d.repository_id;
        SELECT coalesce(max(version),0) INTO pv FROM public.project_policies WHERE project_id=d.project_id;
        ctx=hub.code_audit_task_context(d.task_id);
        fingerprint=encode(sha256(convert_to(concat_ws(':',d.id,p.version,rv,dv,pv,ctx::text),'UTF8')),'hex');
        INSERT INTO public.code_audits(project_id,repository_id,task_id,worklog_id,delivery_id,policy_version,
          repository_version,delivery_policy_version,project_policy_version,git_commit,source_hash,task_context)
          VALUES(d.project_id,d.repository_id,d.task_id,d.worklog_id,d.id,p.version,rv,dv,pv,d.git_commit,fingerprint,ctx)
          ON CONFLICT(source_hash) DO NOTHING RETURNING id INTO jid;
        IF jid IS NULL THEN SELECT id INTO jid FROM public.code_audits WHERE source_hash=fingerprint; END IF;
        RETURN jid;
      END $$;
      REVOKE ALL ON FUNCTION hub.enqueue_code_audit(uuid) FROM PUBLIC;

      CREATE FUNCTION hub.request_code_audit(did uuid) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE d code_deliveries%ROWTYPE;
      BEGIN
        SELECT * INTO d FROM public.code_deliveries WHERE id=did;
        IF d.id IS NULL OR NOT hub.can_read_scope(d.project_id,d.repository_id)
          OR NOT (hub.has_project_cap(d.project_id,'tasks:write') OR
        hub.has_project_cap(d.project_id,'tasks:review')
          OR hub.has_project_cap(d.project_id,'tasks:publish')) THEN
          RAISE EXCEPTION 'audit not accessible' USING ERRCODE='42501'; END IF;
        RETURN hub.enqueue_code_audit(did);
      END $$;
      REVOKE ALL ON FUNCTION hub.request_code_audit(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.request_code_audit(uuid) TO hub_runtime;

      CREATE FUNCTION hub.queue_code_audits() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE did uuid;
      BEGIN
        IF TG_TABLE_NAME='code_deliveries' THEN PERFORM hub.enqueue_code_audit(NEW.id); RETURN NEW; END IF;
        FOR did IN SELECT DISTINCT ON (d.task_id) d.id FROM public.code_deliveries d
          JOIN public.tasks t ON t.id=d.task_id
          WHERE t.status NOT IN ('completed','cancelled') AND
            CASE WHEN TG_TABLE_NAME IN ('code_audit_policies','delivery_policies') THEN
        d.repository_id=(to_jsonb(NEW)->>'repository_id')::uuid
              WHEN TG_TABLE_NAME='project_repositories' THEN d.repository_id=(to_jsonb(NEW)->>'id')::uuid
              WHEN TG_TABLE_NAME='project_policies' THEN d.project_id=(to_jsonb(NEW)->>'project_id')::uuid
              ELSE d.task_id=(to_jsonb(NEW)->>'id')::uuid END
          ORDER BY d.task_id,d.created_at DESC,d.id DESC
        LOOP PERFORM hub.enqueue_code_audit(did); END LOOP;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.queue_code_audits() FROM PUBLIC;
      CREATE TRIGGER queue_code_delivery_audit AFTER INSERT ON code_deliveries
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_policy_audit AFTER INSERT ON code_audit_policies
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_ci_policy_audit AFTER INSERT ON delivery_policies
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_repository_audit AFTER UPDATE OF version ON project_repositories
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_project_policy_audit AFTER INSERT ON project_policies
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();
      CREATE TRIGGER queue_code_task_audit AFTER UPDATE OF
        title,description,acceptance,scope,execution_role ON tasks
        FOR EACH ROW EXECUTE FUNCTION hub.queue_code_audits();

      CREATE FUNCTION hub.code_audit_current(jid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.code_audits j JOIN public.tasks t ON t.id=j.task_id
          JOIN public.project_repositories r ON r.id=j.repository_id
          WHERE j.id=jid AND hub.can_read_scope(j.project_id,j.repository_id)
          AND j.repository_version=r.version AND r.status='active'
          AND j.task_context=hub.code_audit_task_context(t.id)
          AND j.policy_version=(SELECT max(version) FROM public.code_audit_policies WHERE repository_id=r.id)
          AND (SELECT (content->>'enabled')::boolean FROM public.code_audit_policies
            WHERE repository_id=r.id ORDER BY version DESC LIMIT 1)
          AND j.delivery_policy_version=(SELECT coalesce(max(version),0) FROM public.delivery_policies
        WHERE repository_id=r.id)
          AND j.project_policy_version=(SELECT coalesce(max(version),0) FROM public.project_policies
        WHERE project_id=j.project_id)
          AND j.delivery_id=(SELECT d.id FROM public.code_deliveries d WHERE d.task_id=t.id
            ORDER BY d.created_at DESC,d.id DESC LIMIT 1)
          AND (t.status NOT IN ('review','testing','release_ready','completed') OR
        t.submission_id=j.worklog_id))
      $$;
      REVOKE ALL ON FUNCTION hub.code_audit_current(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.code_audit_current(uuid) TO hub_runtime;

      CREATE FUNCTION hub.code_audit_ready(tid uuid) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE t tasks%ROWTYPE; p code_audit_policies%ROWTYPE; j code_audits%ROWTYPE;
        config jsonb; cv integer; workflow text; received delivery_evidence%ROWTYPE;
      BEGIN
        SELECT * INTO t FROM public.tasks WHERE id=tid;
        IF t.id IS NULL OR NOT hub.can_read_scope(t.project_id,t.repository_id) THEN RETURN false; END IF;
        SELECT * INTO p FROM public.code_audit_policies WHERE repository_id=t.repository_id ORDER BY
        version DESC LIMIT 1;
        IF p.repository_id IS NULL OR NOT (p.content->>'enabled')::boolean THEN RETURN true; END IF;
        SELECT * INTO j FROM public.code_audits WHERE task_id=t.id AND worklog_id=t.submission_id
          AND hub.code_audit_current(id) ORDER BY created_at DESC,id DESC LIMIT 1;
        IF j.id IS NULL OR j.state<>'completed' OR NOT EXISTS(SELECT 1 FROM public.code_audit_reports
          WHERE job_id=j.id AND lease_epoch=j.lease_epoch AND passed) THEN RETURN false; END IF;
        SELECT content,version INTO config,cv FROM public.delivery_policies WHERE
        repository_id=t.repository_id
          ORDER BY version DESC LIMIT 1;
        IF config IS NULL OR NOT config->'required_kinds' @> '["ci"]'::jsonb
          OR jsonb_array_length(config->'workflows')=0 OR config->>'ci_target'<>'delivered' THEN RETURN
        false; END IF;
        FOR workflow IN SELECT jsonb_array_elements_text(config->'workflows') LOOP
          SELECT * INTO received FROM public.delivery_evidence e WHERE e.delivery_id=j.delivery_id AND
        e.kind='ci'
            AND e.policy_version=cv AND e.repository_version=j.repository_version AND
        e.observation->>'workflow'=workflow
            ORDER BY e.observation->>'observed_at' DESC,e.created_at DESC,e.id LIMIT 1;
          IF received.id IS NULL OR NOT received.successful
            OR received.observation->>'verified_commit' IS DISTINCT FROM j.git_commit
            OR (received.observation->>'observed_at')::timestamptz<now()-
              make_interval(secs=>(config->>'max_age_seconds')::integer)
          THEN RETURN false; END IF;
        END LOOP;
        RETURN true;
      END $$;
      REVOKE ALL ON FUNCTION hub.code_audit_ready(uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.code_audit_ready(uuid) TO hub_runtime;

      CREATE FUNCTION hub.guard_code_audit_update() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE uid uuid:=nullif(current_setting('hub.user_id',true),'')::uuid;
        did uuid:=nullif(current_setting('hub.device_id',true),'')::uuid; claiming boolean; own boolean;
      BEGIN
        IF NOT hub.can_read_scope(NEW.project_id,NEW.repository_id)
          OR NOT hub.has_project_cap(NEW.project_id,'tasks:review') OR NOT
        hub.has_project_cap(NEW.project_id,'logs:read')
          OR NEW.version<>OLD.version+1 THEN RAISE EXCEPTION 'invalid audit transition' USING
        ERRCODE='23514'; END IF;
        claiming=NEW.state='running' AND NEW.lease_epoch=OLD.lease_epoch+1
          AND (OLD.state='queued' OR OLD.state='running' AND OLD.lease_until<=now());
        own=(OLD.reviewer_id,OLD.device_id) IS NOT DISTINCT FROM (uid,did);
        IF claiming THEN
          IF NOT hub.code_audit_current(NEW.id) OR NEW.reviewer_id<>uid OR NEW.device_id<>did OR
        NEW.instance_id IS NULL
            OR NEW.lease_until<=now() OR NEW.lease_until>now()+interval '16 minutes' THEN
            RAISE EXCEPTION 'invalid audit claim' USING ERRCODE='23514'; END IF;
          IF EXISTS(SELECT 1 FROM public.worklogs WHERE task_id=NEW.task_id AND author_id=uid) THEN
            IF NOT hub.is_superadmin() OR length(trim(NEW.override_reason))<10 THEN
              RAISE EXCEPTION 'independent audit required' USING ERRCODE='42501'; END IF;
          ELSIF NEW.override_reason<>'' THEN RAISE EXCEPTION 'invalid self review override' USING
        ERRCODE='23514'; END IF;
        ELSE
          IF (NEW.reviewer_id,NEW.device_id,NEW.instance_id,NEW.lease_epoch,NEW.override_reason)
              IS DISTINCT FROM
        (OLD.reviewer_id,OLD.device_id,OLD.instance_id,OLD.lease_epoch,OLD.override_reason)
          THEN RAISE EXCEPTION 'audit lease immutable' USING ERRCODE='23514'; END IF;
          IF NEW.state='queued' THEN
            IF length(trim(NEW.reason))<10 OR OLD.state='running' AND OLD.lease_until>now() AND NOT own
              OR OLD.state='completed' AND NOT hub.has_project_cap(NEW.project_id,'tasks:publish') THEN
              RAISE EXCEPTION 'invalid audit retry' USING ERRCODE='23514'; END IF;
          ELSIF NOT own OR OLD.state<>'running' OR OLD.lease_until<=now() OR NOT
        hub.code_audit_current(NEW.id) THEN
            RAISE EXCEPTION 'audit lease expired' USING ERRCODE='23514';
          ELSIF NEW.state='completed' THEN
            IF NOT EXISTS(SELECT 1 FROM public.code_audit_reports WHERE job_id=NEW.id AND
        lease_epoch=NEW.lease_epoch) THEN
              RAISE EXCEPTION 'audit report required' USING ERRCODE='23514'; END IF;
          ELSIF NEW.state<>'running' OR NEW.lease_until<=now() OR NEW.lease_until>now()+interval
        '16 minutes' THEN
            RAISE EXCEPTION 'invalid audit heartbeat' USING ERRCODE='23514';
          END IF;
        END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_code_audit_update() FROM PUBLIC;
      CREATE TRIGGER code_audit_update_guard BEFORE UPDATE ON code_audits
        FOR EACH ROW EXECUTE FUNCTION hub.guard_code_audit_update();

      CREATE FUNCTION hub.guard_code_audit_report() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE j code_audits%ROWTYPE; config jsonb; paths jsonb; checks jsonb; rules jsonb; ok boolean;
      BEGIN
        SELECT * INTO j FROM public.code_audits WHERE id=NEW.job_id FOR UPDATE;
        IF j.state<>'running' OR j.lease_until<=now() OR NOT hub.code_audit_current(j.id)
          OR (j.reviewer_id,j.device_id,j.instance_id,j.lease_epoch,j.source_hash) IS DISTINCT FROM
            (NEW.author_id,NEW.device_id,NEW.instance_id,NEW.lease_epoch,NEW.source_hash)
          OR NEW.author_id IS DISTINCT FROM nullif(current_setting('hub.user_id',true),'')::uuid
          OR NEW.device_id IS DISTINCT FROM nullif(current_setting('hub.device_id',true),'')::uuid
          OR NOT hub.has_project_cap(j.project_id,'tasks:review') OR NOT
        hub.has_project_cap(j.project_id,'logs:read')
        THEN RAISE EXCEPTION 'invalid audit report lease' USING ERRCODE='42501'; END IF;
        SELECT content INTO config FROM public.code_audit_policies WHERE repository_id=j.repository_id
        AND version=j.policy_version;
        SELECT changed_paths INTO paths FROM public.worklogs WHERE id=j.worklog_id;
        SELECT coalesce(jsonb_object_agg(v->>'id',true),'{}') INTO rules FROM
        jsonb_array_elements(config->'rules') v;
        rules=rules || jsonb_build_object('scope',true,'security',true,'correctness',true);
        checks=NEW.content->'rule_checks';
        IF octet_length(NEW.content::text)>131072 OR NEW.content->>'checkout_commit' IS DISTINCT FROM
        j.git_commit
          OR jsonb_typeof(checks) IS DISTINCT FROM 'object' OR
        jsonb_typeof(NEW.content->'reviewed_paths') IS DISTINCT FROM 'array'
          OR jsonb_typeof(NEW.content->'gaps') IS DISTINCT FROM 'array'
          OR jsonb_typeof(NEW.content->'findings') IS DISTINCT FROM 'array'
          OR length(trim(NEW.content->>'summary')) NOT BETWEEN 10 AND 4000
          OR length(trim(NEW.content->>'verification')) NOT BETWEEN 10 AND 4000
          OR NEW.content->>'conclusion' NOT IN ('pass','fail','incomplete')
          OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(checks) key) IS DISTINCT FROM
             (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(rules) key)
          OR EXISTS(SELECT 1 FROM jsonb_each_text(checks) WHERE value NOT IN
        ('pass','fail','unverified','not_applicable'))
          OR EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.content->'findings') f
            WHERE f->>'severity' IS NULL OR f->>'severity' NOT IN ('critical','high','medium','low','info')
              OR f->>'rule_id' IS NULL OR NOT rules ? (f->>'rule_id')
              OR jsonb_typeof(f->'path') IS DISTINCT FROM 'string'
              OR jsonb_typeof(f->'line') IS DISTINCT FROM 'number'
              OR length(trim(f->>'evidence')) NOT BETWEEN 10 AND 2000
              OR length(trim(f->>'recommendation')) NOT BETWEEN 10 AND 2000)
        THEN RAISE EXCEPTION 'invalid audit report content' USING ERRCODE='23514'; END IF;
        ok=NEW.content->>'conclusion'='pass' AND NEW.content->>'source_basis'='exact_checkout'
          AND jsonb_array_length(NEW.content->'gaps')=0 AND NEW.content->'reviewed_paths' @> paths
          AND NOT EXISTS(SELECT 1 FROM jsonb_each_text(checks) WHERE value IN ('fail','unverified'))
          AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.content->'findings') f
            WHERE config->'blocking_severities' ? (f->>'severity'));
        IF NEW.passed IS DISTINCT FROM ok OR NEW.content->>'conclusion'='pass' AND NOT ok THEN
          RAISE EXCEPTION 'incomplete audit cannot pass' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_code_audit_report() FROM PUBLIC;
      CREATE TRIGGER code_audit_report_guard BEFORE INSERT ON code_audit_reports
        FOR EACH ROW EXECUTE FUNCTION hub.guard_code_audit_report();

      CREATE FUNCTION hub.record_code_audit_event() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
      DECLARE uid uuid; did uuid; d code_deliveries%ROWTYPE; label text;
      BEGIN
        SELECT * INTO d FROM public.code_deliveries WHERE id=NEW.delivery_id;
        uid=coalesce(nullif(current_setting('hub.user_id',true),'')::uuid,d.author_id);
        did=coalesce(nullif(current_setting('hub.device_id',true),'')::uuid,d.device_id);
        label=CASE WHEN TG_OP='INSERT' THEN 'queued' WHEN NEW.state='queued' THEN 'retry'
          WHEN NEW.state='completed' THEN 'completed' WHEN NEW.lease_epoch>OLD.lease_epoch THEN 'claimed'
        ELSE 'heartbeat' END;
        INSERT INTO public.code_audit_events(job_id,author_id,device_id,kind,version,payload)
          VALUES(NEW.id,uid,did,label,NEW.version,jsonb_build_object('state',NEW.state,'lease_epoch',NEW.lease_epoch,
          'reason',NEW.reason,'self_review',NEW.override_reason<>'','override_reason',NEW.override_reason));
        IF label<>'heartbeat' THEN
          INSERT INTO
        public.project_progress(project_id,repository_id,author_id,device_id,status,summary,detail,
            task_reference,git_commit,kind,task_id,worklog_id)
          VALUES(NEW.project_id,NEW.repository_id,uid,did,'checkpoint','代码规范审计 · '||label,NEW.reason,
            NEW.task_id::text,NEW.git_commit,'code_audit.'||label,NEW.task_id,NEW.worklog_id);
        END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.record_code_audit_event() FROM PUBLIC;
      CREATE TRIGGER code_audit_events AFTER INSERT OR UPDATE ON code_audits
        FOR EACH ROW EXECUTE FUNCTION hub.record_code_audit_event();

      CREATE FUNCTION hub.guard_code_audit_gate() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $$
      BEGIN
        IF NEW.status IN ('testing','release_ready','completed') AND NEW.status<>OLD.status
          AND NOT hub.code_audit_ready(NEW.id) THEN
          RAISE EXCEPTION 'CODE_AUDIT_REQUIRED' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION hub.guard_code_audit_gate() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.guard_code_audit_gate() TO hub_runtime;
      CREATE TRIGGER code_audit_gate_guard BEFORE UPDATE OF status ON tasks
        FOR EACH ROW EXECUTE FUNCTION hub.guard_code_audit_gate();;

UPDATE alembic_version SET version_num='0027' WHERE alembic_version.version_num = '0026';

-- Running upgrade 0027 -> 0028

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
      END $migration$;;

UPDATE alembic_version SET version_num='0028' WHERE alembic_version.version_num = '0027';

-- Running upgrade 0028 -> 0029

CREATE TABLE account_recovery_codes (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        batch_id uuid NOT NULL, user_epoch integer NOT NULL CHECK(user_epoch>0),
        digest varchar(64) NOT NULL UNIQUE CHECK(digest~'^[a-f0-9]{64}$'),
        expires_at timestamptz NOT NULL, consumed_at timestamptz
      );
      CREATE INDEX ix_recovery_codes_user ON account_recovery_codes(user_id,batch_id);
      ALTER TABLE account_recovery_codes ENABLE ROW LEVEL SECURITY;
      ALTER TABLE account_recovery_codes FORCE ROW LEVEL SECURITY;
      CREATE POLICY owner_access ON account_recovery_codes TO hub_owner USING(true) WITH CHECK(true);
      GRANT SELECT,INSERT ON account_recovery_codes TO hub_runtime;
      GRANT UPDATE(consumed_at) ON account_recovery_codes TO hub_runtime;
      CREATE POLICY recovery_own_read ON account_recovery_codes FOR SELECT TO hub_runtime
        USING(user_id=nullif(current_setting('hub.user_id',true),'')::uuid);
      CREATE POLICY recovery_own_insert ON account_recovery_codes FOR INSERT TO hub_runtime WITH CHECK(
        user_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND hub.account_closeout_allowed(user_id));
      CREATE POLICY recovery_own_retire ON account_recovery_codes FOR UPDATE TO hub_runtime
        USING(user_id=nullif(current_setting('hub.user_id',true),'')::uuid)
        WITH CHECK(user_id=nullif(current_setting('hub.user_id',true),'')::uuid);
      CREATE FUNCTION hub.offline_recovery_user(code_hash text) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT c.user_id FROM public.account_recovery_codes c JOIN public.users u ON u.id=c.user_id
          WHERE c.digest=code_hash AND c.consumed_at IS NULL AND c.expires_at>now()
          AND c.user_epoch=u.auth_epoch AND u.status='active'
      $$;
      REVOKE ALL ON FUNCTION hub.offline_recovery_user(text) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.offline_recovery_user(text) TO hub_runtime;
      DO $migration$
      DECLARE definition text; marker text := 'SELECT EXISTS(';
      BEGIN
        SELECT pg_get_functiondef('hub.account_closeout_allowed(uuid)'::regprocedure) INTO definition;
        IF strpos(definition,marker)=0 THEN RAISE EXCEPTION 'Unknown recovery authorization'; END IF;
        definition=replace(definition,marker,
          'SELECT uid=hub.offline_recovery_user(current_setting(''hub.offline_recovery_digest'',true)) OR EXISTS(');
        EXECUTE definition;
      END $migration$;
      CREATE FUNCTION hub.consume_offline_recovery(code_hash text) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE cid uuid;
      BEGIN
        IF code_hash IS DISTINCT FROM current_setting('hub.offline_recovery_digest',true)
          OR hub.offline_recovery_user(code_hash) IS NULL THEN RETURN false; END IF;
        UPDATE public.account_recovery_codes SET consumed_at=now() WHERE digest=code_hash
          AND consumed_at IS NULL AND expires_at>now() RETURNING id INTO cid;
        RETURN cid IS NOT NULL;
      END $$;
      REVOKE ALL ON FUNCTION hub.consume_offline_recovery(text) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.consume_offline_recovery(text) TO hub_runtime;;

UPDATE alembic_version SET version_num='0029' WHERE alembic_version.version_num = '0028';

-- Running upgrade 0029 -> 0030

CREATE TABLE oauth_clients (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        client_id varchar(100) NOT NULL UNIQUE, name varchar(200) NOT NULL,
        author_id uuid NOT NULL REFERENCES users(id), approver_device_id uuid NOT NULL REFERENCES devices(id),
        user_id uuid NOT NULL REFERENCES users(id), device_id uuid NOT NULL REFERENCES devices(id),
        grant_id uuid REFERENCES access_grants(id), project_id uuid NOT NULL REFERENCES projects(id),
        repository_id uuid NOT NULL REFERENCES project_repositories(id), redirect_uris jsonb NOT NULL,
        scopes jsonb NOT NULL, version integer NOT NULL DEFAULT 1 CHECK(version>0),
        status varchar(16) NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
        CHECK(jsonb_typeof(redirect_uris)='array' AND jsonb_array_length(redirect_uris) BETWEEN 1 AND 5),
        CHECK(jsonb_typeof(scopes)='array' AND scopes <@ '["mcp:read","mcp:coordinate"]'::jsonb),
        FOREIGN KEY(repository_id,project_id) REFERENCES project_repositories(id,project_id)
      );
      CREATE TABLE oauth_requests (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        client_id uuid NOT NULL REFERENCES oauth_clients(id), client_version integer NOT NULL,
        redirect_uri varchar(2000) NOT NULL, resource varchar(1000) NOT NULL, scopes jsonb NOT NULL,
        state varchar(2048) NOT NULL, challenge varchar(43) NOT NULL, expires_at timestamptz NOT NULL,
        session_id uuid REFERENCES auth_sessions(id), code_digest varchar(64) UNIQUE,
        decided_at timestamptz, consumed_at timestamptz
      );
      CREATE TABLE oauth_sessions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        client_id uuid NOT NULL REFERENCES oauth_clients(id), client_version integer NOT NULL,
        session_id uuid NOT NULL REFERENCES auth_sessions(id), resource varchar(1000) NOT NULL,
        scopes jsonb NOT NULL, family_id uuid NOT NULL, access_digest varchar(64) NOT NULL UNIQUE,
        refresh_digest varchar(64) NOT NULL UNIQUE, access_expires_at timestamptz NOT NULL,
        refresh_expires_at timestamptz NOT NULL,
        state varchar(16) NOT NULL DEFAULT 'active' CHECK(state IN ('active','rotated','revoked'))
      );
      CREATE INDEX ix_oauth_session_family ON oauth_sessions(family_id);
      CREATE INDEX ix_oauth_request_expiry ON oauth_requests(expires_at);
      GRANT SELECT,INSERT ON oauth_clients TO hub_runtime;
      GRANT UPDATE(status,version) ON oauth_clients TO hub_runtime;
      GRANT SELECT,INSERT ON oauth_requests,oauth_sessions TO hub_runtime;
      GRANT UPDATE(session_id,code_digest,decided_at,consumed_at) ON oauth_requests TO hub_runtime;
      GRANT UPDATE(state) ON oauth_sessions TO hub_runtime;;

ALTER TABLE oauth_clients ENABLE ROW LEVEL SECURITY;

ALTER TABLE oauth_clients FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON oauth_clients TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE oauth_requests ENABLE ROW LEVEL SECURITY;

ALTER TABLE oauth_requests FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON oauth_requests TO hub_owner USING(true) WITH CHECK(true);

ALTER TABLE oauth_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE oauth_sessions FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON oauth_sessions TO hub_owner USING(true) WITH CHECK(true);

CREATE POLICY oauth_client_read ON oauth_clients FOR SELECT TO hub_runtime USING(true);
      CREATE POLICY oauth_client_insert ON oauth_clients FOR INSERT TO hub_runtime WITH CHECK(
        hub.is_superadmin() AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND approver_device_id=nullif(current_setting('hub.device_id',true),'')::uuid);
      CREATE POLICY oauth_client_revoke ON oauth_clients FOR UPDATE TO hub_runtime
        USING(hub.is_superadmin()) WITH CHECK(hub.is_superadmin());
      -- Protocol storage contains hashes and fixed approval references. Public code
      -- exchange needs lookup before an authenticated context exists, like auth_sessions.
      CREATE POLICY oauth_protocol ON oauth_requests TO hub_runtime USING(true) WITH CHECK(true);
      CREATE POLICY oauth_protocol ON oauth_sessions TO hub_runtime USING(true) WITH CHECK(true);;

UPDATE alembic_version SET version_num='0030' WHERE alembic_version.version_num = '0029';

-- Running upgrade 0030 -> 0031

ALTER TABLE tasks ADD COLUMN workflow_runtime jsonb NOT NULL DEFAULT '{}';
      CREATE TABLE task_workflow_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects(id), task_id uuid NOT NULL,
        cycle integer NOT NULL, revision integer NOT NULL, kind varchar(32) NOT NULL,
        reason varchar(40) NOT NULL, snapshot jsonb NOT NULL,
        author_id uuid REFERENCES users(id), device_id uuid REFERENCES devices(id),
        resolution text NOT NULL DEFAULT '',
        FOREIGN KEY(task_id,project_id) REFERENCES tasks(id,project_id),
        UNIQUE(task_id,revision),
        CHECK(kind IN ('manual_required','resumed')),
        CHECK((author_id IS NULL)=(device_id IS NULL))
      );
      CREATE INDEX workflow_history ON task_workflow_events(project_id,task_id,created_at,id);
      ALTER TABLE task_workflow_events ENABLE ROW LEVEL SECURITY;
      ALTER TABLE task_workflow_events FORCE ROW LEVEL SECURITY;
      CREATE POLICY workflow_owner ON task_workflow_events TO hub_owner USING(true) WITH CHECK(true);
      CREATE POLICY workflow_read ON task_workflow_events FOR SELECT TO hub_runtime USING(
        (hub.has_project_cap(project_id,'logs:read') OR author_id=nullif(current_setting('hub.user_id',true),'')::uuid)
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=task_id AND t.project_id=task_workflow_events.project_id));
      GRANT SELECT ON task_workflow_events TO hub_runtime;
      ALTER TABLE project_progress ALTER COLUMN author_id DROP NOT NULL;
      ALTER TABLE project_progress ALTER COLUMN device_id DROP NOT NULL;
      ALTER TABLE project_progress ADD CONSTRAINT progress_system_identity CHECK(
        (author_id IS NOT NULL AND device_id IS NOT NULL) OR
        (author_id IS NULL AND device_id IS NULL AND kind='task.manual_required'));

      -- Runtime may change business columns, never its counters, deadlines or recovery cycle.
      REVOKE UPDATE ON tasks FROM hub_runtime;
      GRANT UPDATE(project_id,repository_id,parent_id,created_by,title,description,acceptance,scope,
        execution_mode,execution_role,role_profile,workflow,workflow_reason,workflow_policy,documentation,
        status,admission_step,version,lease_epoch,active_run_id,lease_until,submission_id) ON tasks TO hub_runtime;

      CREATE FUNCTION hub.workflow_stage(t public.tasks) RETURNS text
      LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public AS $$
        SELECT CASE t.status WHEN 'draft' THEN coalesce(
          (t.workflow_policy->'admission'->>t.admission_step),
          (ARRAY['requirements','design','feasibility'])[t.admission_step+1], 'requirements')
          WHEN 'in_progress' THEN 'development' WHEN 'ready' THEN 'queued'
          WHEN 'review' THEN 'review' WHEN 'testing' THEN 'test'
          WHEN 'release_ready' THEN 'release' ELSE 'completed' END
      $$;
      CREATE FUNCTION hub.workflow_due(r jsonb) RETURNS boolean
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
        SELECT coalesce(r->>'state'='active' AND (r->>'deadline_at')::timestamptz<=statement_timestamp(),false)
      $$;
      CREATE FUNCTION hub.workflow_lock(pid uuid) RETURNS void
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      BEGIN
        -- Never wait for a project lock while a legacy SQL caller already holds a row lock.
        IF NOT pg_try_advisory_xact_lock_shared(7206110501) OR NOT pg_try_advisory_xact_lock(
          ('x'||substr(encode(sha256(convert_to('project:'||pid::text,'UTF8')),'hex'),1,16))::bit(64)::bigint)
          THEN RAISE EXCEPTION 'workflow project busy' USING ERRCODE='55P03'; END IF;
      END $$;
      CREATE FUNCTION hub.workflow_event(t public.tasks, k text, why text, uid uuid DEFAULT NULL,
        did uuid DEFAULT NULL, note text DEFAULT '') RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        INSERT INTO public.task_workflow_events(project_id,task_id,cycle,revision,kind,reason,snapshot,
          author_id,device_id,resolution) VALUES(t.project_id,t.id,(t.workflow_runtime->>'cycle')::int,
          (t.workflow_runtime->>'revision')::int,k,why,t.workflow_runtime,uid,did,note);
        INSERT INTO public.project_progress(project_id,repository_id,author_id,device_id,status,summary,
          detail,task_reference,kind,task_id) VALUES(t.project_id,t.repository_id,uid,did,'checkpoint',
          left(CASE k WHEN 'manual_required' THEN '任务已转人工处理 · ' ELSE '负责人已恢复流程 · ' END||t.title,300),
          '',t.id::text,CASE k WHEN 'manual_required' THEN 'task.manual_required' ELSE 'task.workflow_resumed' END,t.id);
        INSERT INTO public.audit_events(id,project_id,repository_id,actor_id,action,object_id,request_id,details)
          VALUES(gen_random_uuid(),t.project_id,t.repository_id,uid,'task.workflow_'||k,t.id::text,
          'workflow:'||t.id::text,jsonb_build_object('cycle',t.workflow_runtime->'cycle',
          'revision',t.workflow_runtime->'revision','reason',why));
      END $$;
      CREATE FUNCTION hub.pause_task_workflow(pid uuid, tid uuid, why text) RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE t public.tasks; r jsonb;
      BEGIN
        PERFORM hub.workflow_lock(pid);
        SELECT * INTO t FROM public.tasks WHERE id=tid AND project_id=pid FOR UPDATE;
        r:=t.workflow_runtime;
        IF r->>'state'<>'active' THEN RETURN r; END IF;
        r:=r||jsonb_build_object('state','manual','reason',why,'paused_at',statement_timestamp(),
          'revision',(r->>'revision')::int+1);
        UPDATE public.tasks SET workflow_runtime=r,version=version+1 WHERE id=tid RETURNING * INTO t;
        PERFORM hub.workflow_event(t,'manual_required',why);
        RETURN r;
      END $$;
      CREATE FUNCTION hub.guard_task_workflow() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE r jsonb; stage text; limits jsonb; config integer;
      BEGIN
        PERFORM hub.workflow_lock(NEW.project_id);
        IF TG_OP='INSERT' THEN
          SELECT content->'workflow_limits',version INTO limits,config FROM public.project_collaboration
            WHERE project_id=NEW.project_id ORDER BY version DESC LIMIT 1;
          limits:=jsonb_build_object('stage_rejections',3,'test_returns',2,'stage_timeout_seconds',86400)||coalesce(limits,'{}');
          IF coalesce(jsonb_typeof(limits)<>'object' OR (limits->>'stage_rejections')::int NOT BETWEEN 1 AND 10
            OR (limits->>'test_returns')::int NOT BETWEEN 1 AND 10
            OR (limits->>'stage_timeout_seconds')::int NOT BETWEEN 60 AND 604800,true)
            THEN RAISE EXCEPTION 'invalid workflow limits'; END IF;
          r:=jsonb_build_object('state','active','cycle',1,'revision',1,'rejections','{}'::jsonb,
            'test_returns',0,'limits',limits,'configuration_version',coalesce(config,0),'stage','');
        ELSE
          -- Only the owner functions can write this column (no runtime column privilege).
          IF NEW.workflow_runtime IS DISTINCT FROM OLD.workflow_runtime THEN RETURN NEW; END IF;
          r:=OLD.workflow_runtime;
          IF r->>'state'='manual' OR hub.workflow_due(r) THEN
            RAISE EXCEPTION 'task requires human resolution' USING ERRCODE='55000';
          END IF;
        END IF;
        stage:=hub.workflow_stage(NEW);
        IF stage='queued' AND r->>'stage'='development' THEN stage:='development'; END IF;
        IF r->>'stage' IS DISTINCT FROM stage THEN
          r:=r||jsonb_build_object('stage',stage,'started_at',statement_timestamp(),
            'deadline_at',CASE WHEN stage IN ('queued','completed') THEN NULL ELSE
              statement_timestamp()+make_interval(secs=>(r->'limits'->>'stage_timeout_seconds')::int) END);
        END IF;
        IF stage='completed' THEN r:=r||'{"state":"completed"}'; END IF;
        NEW.workflow_runtime:=r;
        RETURN NEW;
      END $$;
      CREATE TRIGGER workflow_guard BEFORE INSERT OR UPDATE ON tasks
        FOR EACH ROW EXECUTE FUNCTION hub.guard_task_workflow();
      CREATE INDEX workflow_deadlines ON tasks(((workflow_runtime->>'deadline_at')))
        WHERE workflow_runtime->>'state'='active' AND workflow_runtime->>'deadline_at' IS NOT NULL;

      -- Start a fresh timing baseline on upgrade. Historical failures remain in decisions.
      UPDATE tasks SET workflow_runtime=jsonb_build_object('state',CASE WHEN status='completed' THEN 'completed' ELSE 'active' END,
        'cycle',1,'revision',1,'rejections','{}'::jsonb,'test_returns',0,
        'limits',jsonb_build_object('stage_rejections',3,'test_returns',2,'stage_timeout_seconds',86400),
        'configuration_version',0,'stage',hub.workflow_stage(tasks),'started_at',statement_timestamp(),
        'deadline_at',CASE WHEN status IN ('ready','completed') THEN NULL ELSE statement_timestamp()+interval '24 hours' END);

      CREATE FUNCTION hub.count_task_rejection() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE t public.tasks; r jsonb; n integer; loops integer; why text;
      BEGIN
        PERFORM hub.workflow_lock(NEW.project_id);
        SELECT * INTO t FROM public.tasks WHERE id=NEW.task_id AND project_id=NEW.project_id FOR UPDATE;
        r:=t.workflow_runtime;
        IF r->>'state'='manual' OR hub.workflow_due(r) THEN
          RAISE EXCEPTION 'task requires human resolution' USING ERRCODE='55000'; END IF;
        IF NEW.approved THEN RETURN NEW; END IF;
        n:=coalesce((r->'rejections'->>NEW.gate)::int,0)+1;
        loops:=(r->>'test_returns')::int+CASE WHEN NEW.gate='test' THEN 1 ELSE 0 END;
        r:=jsonb_set(r,ARRAY['rejections',NEW.gate],to_jsonb(n))||jsonb_build_object('test_returns',loops);
        UPDATE public.tasks SET workflow_runtime=r WHERE id=t.id;
        why:=CASE WHEN loops>=(r->'limits'->>'test_returns')::int THEN 'test_returns'
          WHEN n>=(r->'limits'->>'stage_rejections')::int THEN 'stage_rejections' END;
        IF why IS NOT NULL THEN PERFORM hub.pause_task_workflow(t.project_id,t.id,why); END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER workflow_decision AFTER INSERT ON task_decisions
        FOR EACH ROW EXECUTE FUNCTION hub.count_task_rejection();
      CREATE FUNCTION hub.guard_workflow_run() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE r jsonb;
      BEGIN
        IF NEW.state='active' THEN
          PERFORM hub.workflow_lock(NEW.project_id);
          SELECT workflow_runtime INTO r FROM public.tasks WHERE id=NEW.task_id AND project_id=NEW.project_id;
          IF r->>'state'='manual' OR hub.workflow_due(r) THEN
            RAISE EXCEPTION 'task requires human resolution' USING ERRCODE='55000'; END IF;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER workflow_run_guard BEFORE INSERT OR UPDATE ON task_runs
        FOR EACH ROW EXECUTE FUNCTION hub.guard_workflow_run();

      -- Revocation must still be able to end a paused lease. Only this owner function
      -- can stamp runtime; normal callers have no UPDATE privilege on that column.
      DO $$ DECLARE definition text; changed text; BEGIN
        SELECT pg_get_functiondef('hub.closeout_freeze(uuid)'::regprocedure) INTO definition;
        changed:=replace(definition,'UPDATE public.tasks t SET active_run_id=NULL',
          'UPDATE public.tasks t SET workflow_runtime=t.workflow_runtime||jsonb_build_object(''revoked_run'',t.active_run_id),active_run_id=NULL');
        IF changed=definition THEN RAISE EXCEPTION 'closeout freeze shape changed'; END IF;
        EXECUTE changed;
      END $$;
      CREATE FUNCTION hub.check_task_workflow(pid uuid,tid uuid) RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE r jsonb;
      BEGIN
        IF NOT EXISTS(SELECT 1 FROM public.tasks t WHERE t.id=tid AND t.project_id=pid
          AND hub.can_read_scope(pid,t.repository_id)) THEN RETURN NULL; END IF;
        PERFORM hub.workflow_lock(pid);
        SELECT workflow_runtime INTO r FROM public.tasks WHERE id=tid AND project_id=pid;
        IF hub.workflow_due(r) THEN r:=hub.pause_task_workflow(pid,tid,'stage_timeout'); END IF;
        RETURN r;
      END $$;
      CREATE FUNCTION hub.resume_task_workflow(pid uuid,tid uuid,v integer,rev integer,note text) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE t public.tasks; r jsonb; uid uuid; did uuid; stage text;
      BEGIN
        IF NOT hub.has_project_cap(pid,'tasks:publish') THEN RETURN false; END IF;
        uid:=nullif(current_setting('hub.user_id',true),'')::uuid;
        did:=nullif(current_setting('hub.device_id',true),'')::uuid;
        IF uid IS NULL OR did IS NULL OR length(trim(note)) NOT BETWEEN 10 AND 2000 THEN RETURN false; END IF;
        PERFORM hub.workflow_lock(pid);
        SELECT * INTO t FROM public.tasks WHERE id=tid AND project_id=pid FOR UPDATE;
        IF NOT FOUND OR NOT hub.can_read_scope(pid,t.repository_id) OR t.version<>v
          OR (t.workflow_runtime->>'revision')::int<>rev OR t.workflow_runtime->>'state'<>'manual' THEN RETURN false; END IF;
        IF t.active_run_id IS NOT NULL THEN
          UPDATE public.task_runs SET state='expired',ended_at=statement_timestamp()
            WHERE id=t.active_run_id AND state='active';
        END IF;
        IF t.status='in_progress' THEN t.status:='ready'; END IF;
        stage:=hub.workflow_stage(t);
        r:=t.workflow_runtime||jsonb_build_object('state','active','cycle',(t.workflow_runtime->>'cycle')::int+1,
          'revision',(t.workflow_runtime->>'revision')::int+1,'reason',NULL,'paused_at',NULL,
          'rejections','{}'::jsonb,'test_returns',0,'stage',stage,'started_at',statement_timestamp(),
          'deadline_at',CASE WHEN stage IN ('queued','completed') THEN NULL ELSE
            statement_timestamp()+make_interval(secs=>(t.workflow_runtime->'limits'->>'stage_timeout_seconds')::int) END);
        UPDATE public.tasks SET workflow_runtime=r,status=t.status,active_run_id=NULL,lease_until=NULL,
          version=version+1 WHERE id=tid RETURNING * INTO t;
        PERFORM hub.workflow_event(t,'resumed','human_resolution',uid,did,trim(note));
        RETURN true;
      END $$;
      CREATE FUNCTION hub.expire_task_workflows(batch integer DEFAULT 100) RETURNS integer
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE item record; count integer:=0;
      BEGIN
        IF NOT pg_try_advisory_xact_lock_shared(7206110501) THEN RETURN 0; END IF;
        FOR item IN SELECT id,project_id FROM public.tasks WHERE workflow_runtime->>'state'='active'
          AND (workflow_runtime->>'deadline_at')::timestamptz<=statement_timestamp()
          ORDER BY workflow_runtime->>'deadline_at',id LIMIT least(greatest(batch,1),100)*5 LOOP
          IF pg_try_advisory_xact_lock(('x'||substr(encode(sha256(convert_to(
            'project:'||item.project_id::text,'UTF8')),'hex'),1,16))::bit(64)::bigint) THEN
            PERFORM hub.pause_task_workflow(item.project_id,item.id,'stage_timeout'); count:=count+1;
          END IF;
          EXIT WHEN count>=least(greatest(batch,1),100);
        END LOOP;
        RETURN count;
      END $$;
      CREATE FUNCTION hub.workflow_preservation(pid uuid,tid uuid,rid uuid,epoch integer,uid uuid,did uuid) RETURNS boolean
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT EXISTS(SELECT 1 FROM public.tasks t JOIN public.task_runs r ON r.id=t.active_run_id
          WHERE t.project_id=pid AND t.id=tid AND t.status='in_progress' AND t.active_run_id=rid
            AND t.lease_epoch=epoch AND r.user_id=uid AND r.device_id=did AND r.state='active'
            AND (t.workflow_runtime->>'state'='manual' OR hub.workflow_due(t.workflow_runtime))
            AND uid=nullif(current_setting('hub.user_id',true),'')::uuid
            AND did=nullif(current_setting('hub.device_id',true),'')::uuid
            AND hub.has_project_cap(pid,'tasks:write') AND hub.can_read_scope(pid,t.repository_id)
            AND (t.repository_id IS NULL OR hub.has_repository_cap(pid,t.repository_id,'code:write')))
      $$;
      DROP POLICY logs_insert ON worklogs;
      CREATE POLICY logs_insert ON worklogs FOR INSERT TO hub_runtime WITH CHECK(
        hub.has_project_cap(project_id,'tasks:write')
        AND author_id=nullif(current_setting('hub.user_id',true),'')::uuid
        AND device_id=nullif(current_setting('hub.device_id',true),'')::uuid
        AND (EXISTS(SELECT 1 FROM tasks t JOIN task_runs r ON r.id=t.active_run_id
          WHERE t.id=worklogs.task_id AND t.project_id=worklogs.project_id AND t.status='in_progress'
            AND t.lease_until>clock_timestamp() AND t.lease_epoch=worklogs.lease_epoch
            AND r.id=worklogs.run_id AND r.user_id=worklogs.author_id AND r.device_id=worklogs.device_id AND r.state='active')
          OR hub.workflow_preservation(project_id,task_id,run_id,lease_epoch,author_id,device_id)));
      REVOKE ALL ON FUNCTION hub.workflow_stage(public.tasks),hub.workflow_due(jsonb),hub.workflow_lock(uuid),
        hub.workflow_event(public.tasks,text,text,uuid,uuid,text),hub.pause_task_workflow(uuid,uuid,text),
        hub.guard_task_workflow(),hub.guard_workflow_run(),hub.count_task_rejection(),hub.check_task_workflow(uuid,uuid),
        hub.resume_task_workflow(uuid,uuid,integer,integer,text),hub.expire_task_workflows(integer),
        hub.workflow_preservation(uuid,uuid,uuid,integer,uuid,uuid) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION hub.check_task_workflow(uuid,uuid),
        hub.resume_task_workflow(uuid,uuid,integer,integer,text),
        hub.workflow_preservation(uuid,uuid,uuid,integer,uuid,uuid) TO hub_runtime;
      GRANT EXECUTE ON FUNCTION hub.expire_task_workflows(integer) TO hub_memory;;

UPDATE alembic_version SET version_num='0031' WHERE alembic_version.version_num = '0030';

-- Running upgrade 0031 -> 0032

ALTER POLICY card_read ON memory_cards USING (
            CASE WHEN worklog_id IS NOT NULL THEN
              worklog_id IN (SELECT w.id FROM public.worklogs w)
            ELSE document_id IN (SELECT d.id FROM public.document_versions d) END
          );

ALTER POLICY job_read ON memory_jobs USING (
            CASE WHEN worklog_id IS NOT NULL THEN
              worklog_id IN (SELECT w.id FROM public.worklogs w)
            ELSE document_id IN (SELECT d.id FROM public.document_versions d) END
          );

ALTER POLICY embedding_read ON memory_embeddings USING (
        card_id IN (SELECT c.id FROM public.memory_cards c));
      ALTER POLICY knowledge_embedding_read ON knowledge_embeddings USING (
        fact_id IN (SELECT f.id FROM public.memory_facts f));
      ALTER POLICY embedding_job_read ON embedding_jobs USING (
        (card_id IS NOT NULL AND (card_id,project_id) IN (SELECT c.id,c.project_id FROM public.memory_cards c))
        OR (fact_id IS NOT NULL AND (fact_id,project_id) IN (SELECT f.id,f.project_id FROM public.memory_facts f)));;

UPDATE alembic_version SET version_num='0032' WHERE alembic_version.version_num = '0031';

COMMIT;


RESET ROLE;
