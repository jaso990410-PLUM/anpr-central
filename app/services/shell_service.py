from dataclasses import dataclass

from app.models.user import User


@dataclass(frozen=True)
class NavigationItem:
    key: str
    label: str
    icon: str
    href: str | None = None

    @property
    def is_available(self) -> bool:
        return self.href is not None


@dataclass(frozen=True)
class NavigationGroup:
    label: str
    items: tuple[NavigationItem, ...]


_PERMISSION_NAVIGATION = (
    ("Management", "sites", "Sites", "site", "sites.view", None),
    ("Management", "cameras", "Cameras", "camera", "cameras.view", "/cameras"),
    ("Management", "peripheral-presets", "Peripheral Presets", "control-board", "control_board.view", "/peripheral-presets"),
    ("Management", "access-list", "Access List", "access-list", "access_list.view", "/access-list"),
    ("Management", "users", "Users", "users", "users.view", "/users"),
    ("Monitoring", "recognition", "Recognition History", "recognition", "events.view", "/recognition"),
    ("Monitoring", "audit", "Audit Log", "audit", "audit.view", "/audit"),
    ("System", "mqtt-settings", "MQTT Broker", "settings", "settings.manage", "/settings/mqtt"),
)


def permission_codes(user: User) -> frozenset[str]:
    return frozenset(
        permission.code
        for role in user.roles
        for permission in role.permissions
    )


def build_navigation(
    user: User,
    *,
    password_change_only: bool = False,
) -> tuple[NavigationGroup, ...]:
    """Build visible navigation from permissions, not role names."""
    account_item = NavigationItem(
        key="account-security",
        label="Change Password",
        icon="security",
        href="/account/change-password",
    )
    if password_change_only:
        return (NavigationGroup(label="Account", items=(account_item,)),)

    granted = permission_codes(user)
    grouped: dict[str, list[NavigationItem]] = {
        "Overview": [
            NavigationItem(
                key="dashboard",
                label="Dashboard",
                icon="dashboard",
                href="/dashboard",
            )
        ]
    }

    for group, key, label, icon, required_permission, href in _PERMISSION_NAVIGATION:
        if required_permission in granted:
            grouped.setdefault(group, []).append(
                NavigationItem(key=key, label=label, icon=icon, href=href)
            )

    grouped.setdefault("System", []).append(account_item)

    return tuple(
        NavigationGroup(label=label, items=tuple(items))
        for label, items in grouped.items()
        if items
    )


def role_label(user: User) -> str:
    role_names = sorted({role.name for role in user.roles})
    return ", ".join(role_names) if role_names else "No role assigned"
