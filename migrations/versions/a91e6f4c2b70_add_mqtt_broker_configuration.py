"""add MQTT broker configuration

Revision ID: a91e6f4c2b70
Revises: d7c4e8f219ab
Create Date: 2026-10-04 21:15:00.000000

"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "a91e6f4c2b70"
down_revision: Union[str, Sequence[str], None] = "d7c4e8f219ab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mqtt_broker_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("password_ciphertext", sa.Text(), nullable=True),
        sa.Column("client_id", sa.String(length=128), nullable=False),
        sa.Column("heartbeat_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column(
            "tls_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_mqtt_broker_config_singleton"),
        sa.PrimaryKeyConstraint("id", name="pk_mqtt_broker_config"),
    )

    connection = op.get_bind()
    permission_id = connection.execute(
        sa.text("SELECT id FROM permissions WHERE code = :code"),
        {"code": "settings.manage"},
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid.uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO permissions (id, code, description) "
                "VALUES (:id, :code, :description)"
            ),
            {
                "id": permission_id,
                "code": "settings.manage",
                "description": "Manage system settings",
            },
        )
    administrator_role_id = connection.execute(
        sa.text("SELECT id FROM roles WHERE code = :code"),
        {"code": "administrator"},
    ).scalar_one_or_none()
    if administrator_role_id is not None:
        existing = connection.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {
                "role_id": administrator_role_id,
                "permission_id": permission_id,
            },
        ).scalar_one_or_none()
        if existing is None:
            connection.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id)"
                ),
                {
                    "role_id": administrator_role_id,
                    "permission_id": permission_id,
                },
            )


def downgrade() -> None:
    connection = op.get_bind()
    permission_id = connection.execute(
        sa.text("SELECT id FROM permissions WHERE code = :code"),
        {"code": "settings.manage"},
    ).scalar_one_or_none()
    if permission_id is not None:
        connection.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_id = :id"),
            {"id": permission_id},
        )
        connection.execute(
            sa.text("DELETE FROM permissions WHERE id = :id"),
            {"id": permission_id},
        )
    op.drop_table("mqtt_broker_config")
