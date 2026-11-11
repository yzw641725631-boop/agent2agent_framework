"""Allow set-based source authorization while retaining invoker RLS."""
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade():
    # These are the same visible source IDs used by memory_source_visible().
    # Keeping the subqueries in the policy lets PostgreSQL hash each authorized
    # set once per statement instead of re-running the invoker function per card.
    # No definer function, stored authorization list, or cross-request cache.
    for table, policy in (("memory_cards", "card_read"), ("memory_jobs", "job_read")):
        op.execute(f"""
          ALTER POLICY {policy} ON {table} USING (
            CASE WHEN worklog_id IS NOT NULL THEN
              worklog_id IN (SELECT w.id FROM public.worklogs w)
            ELSE document_id IN (SELECT d.id FROM public.document_versions d) END
          )
        """)
    op.execute("""
      ALTER POLICY embedding_read ON memory_embeddings USING (
        card_id IN (SELECT c.id FROM public.memory_cards c));
      ALTER POLICY knowledge_embedding_read ON knowledge_embeddings USING (
        fact_id IN (SELECT f.id FROM public.memory_facts f));
      ALTER POLICY embedding_job_read ON embedding_jobs USING (
        (card_id IS NOT NULL AND (card_id,project_id) IN (SELECT c.id,c.project_id FROM public.memory_cards c))
        OR (fact_id IS NOT NULL AND (fact_id,project_id) IN (SELECT f.id,f.project_id FROM public.memory_facts f)));
    """)


def downgrade():
    for table, policy in (("memory_cards", "card_read"), ("memory_jobs", "job_read")):
        op.execute(f"ALTER POLICY {policy} ON {table} USING (hub.memory_source_visible(worklog_id,document_id))")
    op.execute("""
      ALTER POLICY embedding_read ON memory_embeddings USING (
        EXISTS(SELECT 1 FROM public.memory_cards c WHERE c.id=card_id));
      ALTER POLICY knowledge_embedding_read ON knowledge_embeddings USING (
        EXISTS(SELECT 1 FROM public.memory_facts f WHERE f.id=fact_id));
      ALTER POLICY embedding_job_read ON embedding_jobs USING (
        (card_id IS NOT NULL AND EXISTS(SELECT 1 FROM public.memory_cards c WHERE c.id=card_id AND c.project_id=embedding_jobs.project_id))
        OR (fact_id IS NOT NULL AND EXISTS(SELECT 1 FROM public.memory_facts f WHERE f.id=fact_id AND f.project_id=embedding_jobs.project_id)));
    """)
