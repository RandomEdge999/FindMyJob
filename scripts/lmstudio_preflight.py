from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from findmyjob.core.lmstudio import (
    LMSTUDIO_DEFAULT_HOST,
    lmstudio_available_model_ids,
    probe_lmstudio_base_url,
    resolve_lmstudio_model_id_or_default,
)
from findmyjob.filefirst.advanced_models import load_model_router
from findmyjob.filefirst.workspace import FileWorkspace


def _available_model_ids(payload: dict[str, Any]) -> set[str]:
    return {model_id for model_id in lmstudio_available_model_ids(payload) if model_id}


def run_preflight(workspace: Path) -> tuple[bool, str]:
    ws = FileWorkspace(workspace.resolve())
    ws.ensure()
    router = load_model_router(ws)
    if router is None:
        return False, "LM Studio preflight could not load the model router."
    if router.inspect_launch_profile().transport_mix == "all_remote":
        return False, "Launch contract requires at least one LM Studio local HTTP model profile."

    profiles = [profile for profile in router.list_profiles() if router.transport_mode(profile) == "local_http"]
    if not profiles:
        return False, "LM Studio preflight requires at least one local HTTP model profile."

    catalogs: dict[str, set[str]] = {}
    lines: list[str] = []
    for profile in profiles:
        try:
            resolved = probe_lmstudio_base_url(profile.base_url or LMSTUDIO_DEFAULT_HOST)
        except Exception as exc:  # noqa: BLE001 - startup should fail with the probe detail
            return False, f"{profile.name}: {exc}"
        model_ids = catalogs.setdefault(
            resolved.canonical_base_url,
            _available_model_ids(resolved.models_payload),
        )
        resolved_model_id = resolve_lmstudio_model_id_or_default(profile.model, resolved.models_payload)
        if resolved_model_id is None:
            available = ", ".join(sorted(model_ids)) if model_ids else "no models returned"
            return (
                False,
                f"{profile.name}: configured model `{profile.model}` was not found at {resolved.canonical_base_url}. "
                f"Available: {available}",
            )
        if resolved_model_id == profile.model:
            lines.append(f"{profile.name}: ok ({resolved.canonical_base_url})")
        else:
            lines.append(
                f"{profile.name}: ok ({resolved.canonical_base_url} -> {resolved_model_id})"
            )

    return True, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify LM Studio reachability and configured model presence.")
    parser.add_argument("--workspace", default=".", help="Workspace root")
    args = parser.parse_args(argv)

    ok, detail = run_preflight(Path(args.workspace))
    stream = sys.stdout if ok else sys.stderr
    print(detail, file=stream)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
