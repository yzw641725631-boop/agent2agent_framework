"""Rebuildable local semantic indexes. Never changes original-source permissions."""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

MODEL = "bge-small-zh-v1.5-fp32-75c43b0-cls-mean384s320-v1"


def upgrade():
    op.execute("""
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
      CREATE POLICY embedding_worker ON memory_embeddings TO hub_embedding USING(true) WITH CHECK(true);
    """)
    for name in ("knowledge_embeddings", "embedding_jobs"):
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY embedding_owner ON {name} TO hub_owner USING(true) WITH CHECK(true)")
        op.execute(f"CREATE POLICY embedding_worker ON {name} TO hub_embedding USING(true) WITH CHECK(true)")
    op.execute("""
      CREATE POLICY knowledge_embedding_read ON knowledge_embeddings FOR SELECT TO hub_runtime
        USING(EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=fact_id));
      CREATE POLICY embedding_job_read ON embedding_jobs FOR SELECT TO hub_runtime USING(
        (card_id IS NOT NULL AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.id=card_id AND c.project_id=embedding_jobs.project_id))
        OR (fact_id IS NOT NULL AND EXISTS(SELECT 1 FROM memory_facts f WHERE f.id=fact_id AND f.project_id=embedding_jobs.project_id)));
      CREATE POLICY embedding_job_retry ON embedding_jobs FOR UPDATE TO hub_runtime
        USING(status='failed' AND hub.has_project_cap(project_id,'tasks:publish'))
        WITH CHECK(status='queued' AND attempts=0 AND hub.has_project_cap(project_id,'tasks:publish'));
    """)
    op.execute(f"""
      CREATE FUNCTION hub.queue_embedding() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        IF TG_TABLE_NAME='memory_cards' THEN
          INSERT INTO public.embedding_jobs(project_id,card_id,model,content_hash)
            VALUES(NEW.project_id,NEW.id,'{MODEL}',encode(sha256(convert_to(NEW.title||E'\\n'||NEW.body,'UTF8')),'hex'))
            ON CONFLICT(card_id,model) DO NOTHING;
        ELSIF NEW.status='active' THEN
          INSERT INTO public.embedding_jobs(project_id,fact_id,model,content_hash)
            VALUES(NEW.project_id,NEW.id,'{MODEL}',encode(sha256(convert_to(NEW.topic||E'\\n'||NEW.title||E'\\n'||NEW.body,'UTF8')),'hex'))
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
        SELECT project_id,id,'{MODEL}',encode(sha256(convert_to(title||E'\\n'||body,'UTF8')),'hex') FROM memory_cards;
      INSERT INTO embedding_jobs(project_id,fact_id,model,content_hash)
        SELECT project_id,id,'{MODEL}',encode(sha256(convert_to(topic||E'\\n'||title||E'\\n'||body,'UTF8')),'hex')
          FROM memory_facts WHERE status='active';
    """)


def downgrade():
    raise RuntimeError("Preserve sources and knowledge decisions; roll back application images only")
