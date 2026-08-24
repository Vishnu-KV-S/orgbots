"""034 departments — the table M4 deferred

Revision ID: 034_departments
Revises: 033_identity

M4's deviation table refused to build this, and the reason it gave was correct for
M4: *"a schema change with a runtime blast radius, inside the one milestone whose
claim is that runtime behaviour is untouched."* M5b creates a second department, and
a department that is a text column has no referential integrity at the moment two of
them exist — so it lands here, first, before anything else in M5.

**The sequencing that matters** (M5 §3):

1. `CREATE TABLE departments`
2. Backfill one row per distinct existing text value
3. `actors.department_id`, populated
4. Re-point `budget.department_scope_id` / `memory.department_scope_id`
5. Keep the text column, synced, until M6

Step 4 is the one that would normally be a data migration, and here it is not one.
Both derived ids are *carried as columns on the new row* rather than recomputed:

    departments.id               == budget.department_scope_id(org, name)
    departments.memory_scope_id  == org.department.department_scope_id(org, name)

Those two functions already produce stable `uuid5` values, and every
`budget_pools.scope_id`, `memory_metadata.scope_id` and `RunSpec` in the database was
written from them. Making the new primary key *equal to the value that is already
there* means the re-point moves no rows at all: a foreign key arrives over data that
already agrees. If instead we had minted fresh ids, every M3 memory would have been
orphaned from its department and every budget pool would have needed rewriting under
a live organization.

The two ids are different values because M3 chose a different namespace string, and
this migration does not unify them. Carrying both is honest; rewriting one of them
here would be exactly the blast radius M4 declined.

**Step 5 is the step people skip.** Memory scope strings are stored in
`memory_metadata.scope_id` and embedded in RunSpecs already pinned to running runs.
Dropping the text representation in the same migration that adds the foreign key
breaks a resumed run. So `actors.department` stays, and a trigger keeps it and
`department_id` from ever disagreeing — in both directions, so pre-M5 code that
writes only the text column still produces a consistent row. M6 retires it once
nothing pinned references it.

**`department_id` is nullable, and step 3's "then NOT NULL" is deliberately not
done.** An actor with no department is not a degenerate state to migrate away: it is
the case `BudgetService.ensure_chain` is written around — *"forcing a synthetic pool
in between would put a level in the lock chain that means nothing."* A NOT NULL here
would require inventing a department for every unplaced actor, which is a worse lie
than a null.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "034_departments"
down_revision: str | None = "033_identity"
branch_labels = None
depends_on = None


def _budget_scope_id(organization_id: uuid.UUID, name: str) -> uuid.UUID:
    """Mirror of `runtime.budget.service.department_scope_id`.

    Spelled out rather than imported. A migration that imports application code runs
    whatever that code says *today*, not what it said when the migration was written,
    and a derivation that quietly changed would silently re-key every department in
    every database this has already run against. `tests/test_m5_departments.py`
    asserts the two agree, which is the check that belongs in a test rather than in an
    import.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"department:{organization_id}:{name}")


def _memory_scope_id(organization_id: uuid.UUID, name: str) -> uuid.UUID:
    """Mirror of `runtime.org.department.department_scope_id`. Same argument."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"memscope:department:{organization_id}:{name}")


def upgrade() -> None:
    op.create_table(
        "departments",
        # Not a fresh uuid4: this *is* the budget pool's `scope_id` for this
        # department, which is why re-pointing costs nothing. See the module docstring.
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # Self-referencing and nullable. A department tree is acyclic — the compiler
        # checks that (M4 §6) — and a foreign key cannot express acyclicity, so this
        # is integrity for the edge and nothing more.
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("departments.id"),
            nullable=True,
        ),
        # A *name*, not an id, for the reason `actors.reports_to` is a name (033):
        # the compiler resolves the whole graph in memory and checks it before writing
        # anything, so a foreign key here would buy integrity we already have and cost
        # the two-phase apply its freedom to write in any order.
        sa.Column("head_actor_name", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        # M3's derivation, carried rather than recomputed. Unique because a memory
        # scope that addressed two departments would be a silent cross-scope read,
        # which is eval 5 and a zero-tolerance gate.
        sa.Column("memory_scope_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        # Edge case 78's rule, applied to departments: removed from the YAML means
        # deactivated, never deleted, because memories and budget pools reference it.
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_department_name"),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name="ck_department_parent_self"
        ),
    )

    # --- step 2: backfill one row per distinct existing text value -------------------
    #
    # In Python rather than in SQL because the ids are `uuid5` values and core Postgres
    # has no SHA-1: `pgcrypto`'s `digest()` would do it, and requiring an extension for
    # a one-time backfill is a deployment dependency bought for nothing.
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            """
            SELECT DISTINCT organization_id, department FROM actors
             WHERE department IS NOT NULL AND department <> ''
            UNION
            SELECT DISTINCT organization_id, department FROM roles
             WHERE department IS NOT NULL AND department <> ''
            """
        )
    ).all()
    for organization_id, name in existing:
        bind.execute(
            sa.text(
                """
                INSERT INTO departments (id, organization_id, name, memory_scope_id)
                VALUES (:id, :org, :name, :mem)
                ON CONFLICT ON CONSTRAINT uq_department_name DO NOTHING
                """
            ),
            {
                "id": _budget_scope_id(organization_id, name),
                "org": organization_id,
                "name": name,
                "mem": _memory_scope_id(organization_id, name),
            },
        )

    # --- step 3: the foreign key from actors ----------------------------------------
    op.add_column(
        "actors",
        sa.Column(
            "department_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("departments.id"),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE actors a
               SET department_id = d.id
              FROM departments d
             WHERE d.organization_id = a.organization_id AND d.name = a.department
            """
        )
    )
    op.create_index("ix_actors_department", "actors", ["department_id"])

    # --- step 5: keep the two representations from ever disagreeing ------------------
    #
    # Both directions, and the id→text direction is the one that will matter in M6.
    # Right now the text is what everything writes; after M6 it will be the id, and a
    # trigger that only handled text→id would have to be rewritten at exactly the
    # moment the old column stops being maintained.
    #
    # A text value with no `departments` row leaves `department_id` NULL rather than
    # creating one. Creating rows from a trigger would mean a typo in a hand-written
    # UPDATE mints a department, and departments are reviewed objects now — `apply`
    # creates them from a `Department` document or they do not exist.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION sync_actor_department() RETURNS trigger AS $$
            BEGIN
                IF NEW.department_id IS NOT NULL
                   AND (TG_OP = 'INSERT'
                        OR NEW.department_id IS DISTINCT FROM OLD.department_id) THEN
                    SELECT d.name INTO NEW.department
                      FROM departments d WHERE d.id = NEW.department_id;
                ELSIF NEW.department IS NOT NULL THEN
                    SELECT d.id INTO NEW.department_id
                      FROM departments d
                     WHERE d.organization_id = NEW.organization_id AND d.name = NEW.department;
                ELSE
                    NEW.department_id := NULL;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_sync_actor_department
            BEFORE INSERT OR UPDATE OF department, department_id ON actors
            FOR EACH ROW EXECUTE FUNCTION sync_actor_department()
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_sync_actor_department ON actors"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS sync_actor_department()"))
    op.drop_index("ix_actors_department", table_name="actors")
    op.drop_column("actors", "department_id")
    op.drop_table("departments")
