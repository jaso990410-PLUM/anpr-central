from getpass import getpass

from argon2 import PasswordHasher
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.permission import Permission
from app.models.role import Role
from app.models.site import Site
from app.models.user import User


ph = PasswordHasher()


DEFAULT_PERMISSIONS = {
    "users.view": "View users",
    "users.manage": "Create, edit, disable, and manage users",

    "sites.view": "View sites",
    "sites.manage": "Manage sites",

    "cameras.view": "View cameras",
    "cameras.manage": "Manage cameras",

    "events.view": "View ANPR recognition events",

    "audit.view": "View audit logs",
    "settings.manage": "Manage system settings",
    "access_list.view": "View central access-list records and synchronization state",
    "access_list.manage": "Create, edit, enable, and synchronize access-list records",
    "access_list.delete_all_camera": "Delete all access-list records from a camera",
    "control_board.view": "View control-board configuration and peripheral behavior",
    "control_board.manage": "Configure control boards, presets, and schedules",
    "control_board.test": "Send explicit control-board hardware test commands",
    "parking.view": "View parking sessions, decisions, and alerts",
    "parking.manage": "Manage parking policy, profiles, and alerts",
    "barrier.manage": "Configure and commission barrier integrations",
    "barrier.operate": "Operate and test physical barriers",
    "operations.view": "View the lane operations console",
    "recognition.correct": "Correct recognition plates while preserving camera evidence",
    "parking.reconcile": "Reconcile parking sessions and exceptions",
    "vehicle.watch": "Manage site-scoped vehicle watch flags",
    "residential.view": "View residential units, residents, and vehicle relationships",
    "residential.manage": "Manage residential identity and vehicle relationships",
    "visitor.manage": "Create and manage visitor reservations",
    "visitor.approve": "Approve and reject visitor reservations",
}


DEFAULT_ROLES = {
    "administrator": {
        "name": "Administrator",
        "description": "Full system administration access",
        "permissions": list(DEFAULT_PERMISSIONS.keys()),
    },

    "operator": {
        "name": "Operator",
        "description": "Operational management access",
        "permissions": [
            "sites.view",
            "cameras.view",
            "events.view",
        ],
    },

    "security": {
        "name": "Security",
        "description": "Security monitoring access",
        "permissions": [
            "cameras.view",
            "events.view",
        ],
    },

    "viewer": {
        "name": "Viewer",
        "description": "Read-only monitoring access",
        "permissions": [
            "sites.view",
            "cameras.view",
            "events.view",
        ],
    },
}


def get_or_create_site(session):
    site = session.scalar(
        select(Site).where(Site.code == "MAIN")
    )

    if site is None:
        site = Site(
            code="MAIN",
            name="Main Site",
            timezone="Asia/Kuala_Lumpur",
            is_active=True,
        )
        session.add(site)
        session.flush()

        print("Created site: MAIN / Main Site")
    else:
        print("Site MAIN already exists")

    return site


def get_or_create_permissions(session):
    permissions = {}

    for code, description in DEFAULT_PERMISSIONS.items():
        permission = session.scalar(
            select(Permission).where(Permission.code == code)
        )

        if permission is None:
            permission = Permission(
                code=code,
                description=description,
            )
            session.add(permission)
            session.flush()

            print(f"Created permission: {code}")

        permissions[code] = permission

    return permissions


def get_or_create_roles(session, permissions):
    roles = {}

    for code, definition in DEFAULT_ROLES.items():
        role = session.scalar(
            select(Role).where(Role.code == code)
        )

        if role is None:
            role = Role(
                code=code,
                name=definition["name"],
                description=definition["description"],
                is_system=True,
            )
            session.add(role)
            session.flush()

            print(f"Created role: {definition['name']}")

        desired_permissions = [
            permissions[permission_code]
            for permission_code in definition["permissions"]
        ]

        role.permissions = desired_permissions

        roles[code] = role

    return roles


def create_initial_admin(session, administrator_role):
    existing_admin = session.scalar(
        select(User)
        .join(User.roles)
        .where(Role.code == "administrator")
    )

    if existing_admin is not None:
        print()
        print(
            f"Administrator already exists: "
            f"{existing_admin.username}"
        )
        return

    print()
    print("Create the first administrator account")
    print("--------------------------------------")

    while True:
        username = input("Username: ").strip()

        if not username:
            print("Username cannot be empty.")
            continue

        existing_user = session.scalar(
            select(User).where(User.username == username)
        )

        if existing_user is not None:
            print("That username already exists.")
            continue

        break

    while True:
        full_name = input("Full name: ").strip()

        if full_name:
            break

        print("Full name cannot be empty.")

    email = input("Email (optional): ").strip()

    while True:
        password = getpass("Password: ")
        confirm_password = getpass("Confirm password: ")

        if password != confirm_password:
            print("Passwords do not match.")
            continue

        if len(password) < 12:
            print("Use a password with at least 12 characters.")
            continue

        break

    password_hash = ph.hash(password)

    user = User(
        username=username,
        email=email or None,
        full_name=full_name,
        password_hash=password_hash,
        is_active=True,
        must_change_password=False,
        failed_login_count=0,
    )

    user.roles.append(administrator_role)

    session.add(user)

    print()
    print(f"Created administrator: {username}")


def main():
    with SessionLocal() as session:
        try:
            get_or_create_site(session)

            permissions = get_or_create_permissions(session)

            roles = get_or_create_roles(
                session,
                permissions,
            )

            create_initial_admin(
                session,
                roles["administrator"],
            )

            session.commit()

            print()
            print("Bootstrap completed successfully.")

        except Exception:
            session.rollback()
            raise


if __name__ == "__main__":
    main()
