"""Durable rollback snapshots for active Docker limit changes."""

import sqlalchemy as sa


def throttle_table(metadata):
    return sa.Table(
        "pending_throttles",
        metadata,
        sa.Column("container_id", sa.String(64), primary_key=True),
        sa.Column("resource", sa.String(16), primary_key=True),
        sa.Column("snapshot", sa.JSON, nullable=False),
    )


def read_pending(engine, table):
    with engine.connect() as conn:
        return {
            (row.container_id, row.resource): row.snapshot for row in conn.execute(sa.select(table))
        }


def write_pending(engine, table, cid, resource, snapshot):
    with engine.begin() as conn:
        if snapshot is None:
            conn.execute(
                table.delete().where(table.c.container_id == cid, table.c.resource == resource)
            )
        else:
            # Insert only: an existing rollback point must never be overwritten.
            conn.execute(
                table.insert().values(container_id=cid, resource=resource, snapshot=snapshot)
            )
