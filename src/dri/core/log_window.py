"""Bounded SQL reads for windowed log evidence, shared by both backends."""

from dataclasses import dataclass, field

from sqlalchemy import case, func


@dataclass
class LogWindow:
    lines: int = 0
    errors: int = 0
    templates: list = field(default_factory=list)
    samples: list = field(default_factory=list)


def read_log_window(session_factory, record, container_id, since, until):
    with session_factory() as session:
        rows = session.query(record).filter(
            record.container_id == container_id,
            record.timestamp >= since,
            record.timestamp <= until,
        )
        lines, errors = rows.with_entities(
            func.count(), func.count(case((record.level == "ERROR", 1)))
        ).one()
        if not lines:
            return LogWindow()

        # Rank before fetching text so even a frequent template missing from
        # the latest 50 messages has a representative example.
        groups = (
            rows.filter(record.template_id.isnot(None))
            .with_entities(
                record.template_id,
                func.count().label("frequency"),
                func.min(record.timestamp).label("first_seen"),
                func.max(record.id).label("sample_id"),
                func.max(
                    case((record.level == "ERROR", 2), (record.level == "WARN", 1), else_=0)
                ).label("severity"),
            )
            .group_by(record.template_id)
            .order_by(func.count().desc(), record.template_id)
            .limit(10)
            .subquery()
        )
        templates = [
            dict(
                template_id=group_id,
                count=count,
                first_seen=first_seen,
                sample=row.message,
                level={2: "ERROR", 1: "WARN"}.get(severity, row.level),
            )
            for row, group_id, count, first_seen, severity in session.query(
                record,
                groups.c.template_id,
                groups.c.frequency,
                groups.c.first_seen,
                groups.c.severity,
            ).join(groups, record.id == groups.c.sample_id)
        ]
        samples = rows.order_by(record.timestamp.desc(), record.id.desc()).limit(50).all()
        return LogWindow(lines, errors, templates, list(reversed(samples)))
