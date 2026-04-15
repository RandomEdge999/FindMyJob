from __future__ import annotations

SERVICE_NAME = "findmyjob"


def keyring_status() -> dict[str, str | bool | None]:
    try:
        import keyring
    except Exception as exc:
        return {"available": False, "backend": None, "detail": str(exc)}

    try:
        backend = keyring.get_keyring()
        backend_name = f"{backend.__class__.__module__}.{backend.__class__.__name__}"
        available = ".fail." not in backend_name.lower()
        detail = None if available else "keyring is installed but no usable backend is configured"
        return {"available": available, "backend": backend_name, "detail": detail}
    except Exception as exc:
        return {"available": False, "backend": None, "detail": str(exc)}


def get_secret(name: str) -> str | None:
    try:
        import keyring
    except Exception:
        return None

    try:
        return keyring.get_password(SERVICE_NAME, name)
    except Exception:
        return None


def set_secret(name: str, value: str) -> None:
    try:
        import keyring
    except Exception as exc:
        raise RuntimeError("keyring is unavailable; install the optional runtime dependencies first.") from exc

    try:
        keyring.set_password(SERVICE_NAME, name, value)
    except Exception as exc:
        raise RuntimeError(f"failed to store secret `{name}` in the OS keyring: {exc}") from exc
