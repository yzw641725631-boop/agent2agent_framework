"""Bounded multi-source knowledge; every citation retains its original visibility."""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
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
      END $migration$;
    """)


def downgrade():
    raise RuntimeError("Preserve knowledge citation history; restore a verified backup instead")
