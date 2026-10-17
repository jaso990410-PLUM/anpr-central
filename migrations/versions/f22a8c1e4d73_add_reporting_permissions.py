"""add historical reporting permissions

Revision ID: f22a8c1e4d73
Revises: e21a7c9d4f60
Create Date: 2026-10-18
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "f22a8c1e4d73"
down_revision: str | None = "e21a7c9d4f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    insert_permission = sa.text(
        "INSERT INTO permissions (id,code,description) "
        "SELECT :id,:code,:description "
        "WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code=:code)"
    ).bindparams(
        sa.bindparam("id", type_=sa.Uuid()),
        sa.bindparam("code", type_=sa.String(100)),
        sa.bindparam("description", type_=sa.String(255)),
    )
    permissions = {
        "reports.view": "View historical analytics and reports",
        "reports.export": "Export filtered historical reports",
    }
    for code, description in permissions.items():
        connection.execute(insert_permission, {"id": uuid.uuid4(), "code": code, "description": description})
    connection.execute(sa.text("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
        WHERE r.code = 'administrator'
          AND p.code IN ('reports.view', 'reports.export')
          AND NOT EXISTS (
              SELECT 1 FROM role_permissions rp
              WHERE rp.role_id = r.id AND rp.permission_id = p.id
          )
    """))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE code IN ('reports.view','reports.export'))"
    ))
    op.execute(sa.text("DELETE FROM permissions WHERE code IN ('reports.view','reports.export')"))
