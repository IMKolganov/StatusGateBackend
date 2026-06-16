PANEL_ROLE_SLUGS = frozenset({"admin", "operator", "viewer"})
DEFAULT_PUBLIC_ROLE_SLUG = "user"
DEFAULT_BOOTSTRAP_ROLE_SLUG = "admin"


def is_panel_role(slug: str) -> bool:
    return slug in PANEL_ROLE_SLUGS


def account_has_panel_access(role_slugs: list[str]) -> bool:
    return any(is_panel_role(slug) for slug in role_slugs)
