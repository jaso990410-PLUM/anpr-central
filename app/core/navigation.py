from urllib.parse import unquote, urlsplit


def safe_local_redirect(
    destination: str | None,
    *,
    default: str = "/dashboard",
) -> str:
    """Allow only root-relative redirect destinations on this application."""
    if not destination or not destination.startswith("/"):
        return default

    decoded = unquote(destination)
    parsed = urlsplit(destination)
    if (
        destination.startswith("//")
        or decoded.startswith("//")
        or "\\" in destination
        or "\\" in decoded
        or parsed.scheme
        or parsed.netloc
    ):
        return default

    return destination
