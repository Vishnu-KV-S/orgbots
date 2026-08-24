"""020 audit — audit_logs, partitioned by month

Revision ID: 020_audit
Revises: 019_killswitch

`audit_log` (004) records *actions*. This records **decisions**, and the difference
is the point of M2 §2: a gateway that only logs what it did tells you nothing about
what your agents keep trying to do. The denial stream is the valuable half, and §9
makes reading a week of it an exit criterion.

Both tables stay. 004's `audit_log` is the effect-pipeline trail — one row per thing
that happened, joined to `logical_call_id`. This one is one row per *check*, written
by the gateways, and it is much higher volume: every allowed call writes here too,
because a denial rate is meaningless without a denominator.

**Partitioned by month** because that volume is the whole problem. A year of decision
rows on one heap makes the §9 review a sequential scan; monthly partitions make it a
single partition read, and dropping a year-old month is a `DROP TABLE` rather than a
`DELETE` that bloats the table it was supposed to shrink.

The primary key must include the partition key, so it is `(id, occurred_at)` rather
than `id`. `id` alone is still unique in practice — one sequence feeds every
partition — but the constraint cannot say so, and pretending otherwise with a unique
index per partition would be a lie that only shows up on a cross-partition join.

`ensure_audit_partition()` exists so extending the window is one call from an
operator command rather than a migration. The DEFAULT partition is a safety net, not
a plan: rows landing there mean the window was not extended, which is why
`audit_partition_health()` reports its row count.
"""

from __future__ import annotations

from alembic import op

revision: str = "020_audit"
down_revision: str | None = "019_killswitch"
branch_labels = None
depends_on = None

MONTHS_AHEAD = 18
MONTHS_BEHIND = 2


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_logs (
            id              bigserial   NOT NULL,
            organization_id uuid        NOT NULL,
            occurred_at     timestamptz NOT NULL DEFAULT now(),
            run_id          uuid,
            root_run_id     uuid,
            actor_id        uuid,
            actor_name      text,
            gateway         text        NOT NULL,
            subject         text        NOT NULL,
            decision        text        NOT NULL,
            check_name      text,
            reason          text,
            severity        text        NOT NULL DEFAULT 'normal',
            blast_radius    text,
            cost_cents      bigint,
            trace_id        text,
            detail          jsonb       NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (id, occurred_at),
            CONSTRAINT ck_audit_gateway  CHECK (gateway IN ('tool','model','run','approval')),
            CONSTRAINT ck_audit_decision CHECK (
                decision IN ('allowed','denied','approval_required')
            ),
            CONSTRAINT ck_audit_severity CHECK (severity IN ('low','normal','high'))
        ) PARTITION BY RANGE (occurred_at)
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_audit_partition(p_month date)
        RETURNS text LANGUAGE plpgsql AS $$
        DECLARE
            start_at date := date_trunc('month', p_month)::date;
            end_at   date := (date_trunc('month', p_month) + interval '1 month')::date;
            part     text := 'audit_logs_' || to_char(start_at, 'YYYYMM');
        BEGIN
            IF to_regclass(part) IS NULL THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF audit_logs FOR VALUES FROM (%L) TO (%L)',
                    part, start_at, end_at
                );
                -- The two queries the §9 review actually runs: "denials this month"
                -- and "everything for this run". Both are per-partition.
                EXECUTE format(
                    'CREATE INDEX %I ON %I (organization_id, occurred_at DESC) '
                    'WHERE decision <> ''allowed''',
                    part || '_denials', part
                );
                EXECUTE format('CREATE INDEX %I ON %I (run_id)', part || '_run', part);
            END IF;
            RETURN part;
        END $$
        """
    )

    op.execute(
        f"""
        DO $$
        DECLARE m int;
        BEGIN
            FOR m IN -{MONTHS_BEHIND}..{MONTHS_AHEAD} LOOP
                PERFORM ensure_audit_partition(
                    (date_trunc('month', now()) + (m || ' month')::interval)::date
                );
            END LOOP;
        END $$
        """
    )

    # Safety net. A row here means the window was not extended in time; it is not
    # lost, but `audit_partition_health` reports it so somebody notices.
    op.execute("CREATE TABLE audit_logs_default PARTITION OF audit_logs DEFAULT")
    op.execute(
        "CREATE INDEX ix_audit_default_org ON audit_logs_default (organization_id, occurred_at)"
    )

    op.execute(
        """
        CREATE OR REPLACE VIEW v_audit_partition_health AS
        SELECT c.relname                                  AS partition,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
               c.reltuples::bigint                        AS approx_rows,
               c.relname = 'audit_logs_default'           AS is_default
          FROM pg_class c
          JOIN pg_inherits i ON i.inhrelid = c.oid
          JOIN pg_class p ON p.oid = i.inhparent
         WHERE p.relname = 'audit_logs'
         ORDER BY c.relname
        """
    )

    # The denial stream, ready to read. §9 requires this review; making it a view
    # means the query somebody runs at 9am is the query that was reviewed.
    op.execute(
        """
        CREATE OR REPLACE VIEW v_denial_stream AS
        SELECT organization_id,
               actor_name,
               gateway,
               subject,
               check_name,
               reason,
               count(*)          AS denials,
               min(occurred_at)  AS first_seen,
               max(occurred_at)  AS last_seen
          FROM audit_logs
         WHERE decision = 'denied'
         GROUP BY organization_id, actor_name, gateway, subject, check_name, reason
         ORDER BY count(*) DESC
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_denial_stream")
    op.execute("DROP VIEW IF EXISTS v_audit_partition_health")
    op.execute("DROP TABLE IF EXISTS audit_logs")
    op.execute("DROP FUNCTION IF EXISTS ensure_audit_partition(date)")
