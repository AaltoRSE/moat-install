"""Final summary output: brief default listing plus a full verbose table.

The default output describes what was configured — one line per harness with
the providers actually configured for it and the aggregate status. ``--verbose``
additionally prints the full per-provider table, warnings, and manual-action
notes. No containerization details appear in either form.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from .adapters import AdapterReport
from .home import mask_key
from .providers import ProviderProfile


def _status_for(report: AdapterReport, provider: str) -> str:
    return report.provider_status.get(provider, "-")


def render_summary(
    reports: Sequence[AdapterReport],
    selected_providers: Sequence[str],
    dry_run: bool,
) -> str:
    """Render the full summary table and notes as text."""
    lines: List[str] = []
    prefix = "WOULD " if dry_run else ""
    lines.append("")
    lines.append(f"=== moat-install summary{' (dry run)' if dry_run else ''} ===")
    header = (
        f"{'harness':<16} {'provider':<13} {prefix + 'status':<18} "
        f"{'key mode':<9} {'parse':<18} {'files / backups'}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for report in reports:
        if not report.ok:
            lines.append(
                f"{report.harness:<16} {'(all)':<13} {'FAILED':<18} "
                f"{'-':<9} {'-':<18} {report.error or ''}"
            )
            continue
        for provider in selected_providers:
            status = _status_for(report, provider)
            files = "; ".join(report.files_created + report.files_updated + report.files_unchanged)
            backups = "; ".join(report.backups)
            detail = files + (f" [backup: {backups}]" if backups else "")
            lines.append(
                f"{report.harness:<16} {provider:<13} {prefix + status:<18} "
                f"{report.key_mode:<9} {report.parse_check:<18} {detail}"
            )

    warnings = [w for r in reports for w in r.warnings]
    notes = [n for r in reports for n in r.manual_notes]
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"  ! {w}")
    if notes:
        lines.append("")
        lines.append("Manual actions:")
        for n in notes:
            lines.append(f"  * {n}")

    return "\n".join(lines)


def render_brief_summary(
    reports: Sequence[AdapterReport],
    selected_providers: Sequence[str],
    dry_run: bool,
) -> str:
    """Render the concise default summary: what was configured, nothing more.

    The home base is announced before the key prompts, so it is not repeated
    here. One line per harness naming the providers actually configured for it
    and the aggregate status; failures (with their error) are always shown.
    Warnings, manual-action notes, and per-provider detail are ``--verbose``
    material — the CLI prints a suppressed-count hint in their place.
    """
    lines: List[str] = []
    prefix = "WOULD " if dry_run else ""
    lines.append("")
    lines.append(f"=== moat-install summary{' (dry run)' if dry_run else ''} ===")
    lines.append(
        "Providers: "
        + ", ".join(
            f"{name} (default)" if i == 0 else name for i, name in enumerate(selected_providers)
        )
    )
    lines.append("Configured harnesses:" if not dry_run else "Harnesses that would be configured:")
    for report in reports:
        if not report.ok:
            lines.append(f"  {report.harness:<12} FAILED  {report.error or ''}")
            continue
        configured = ", ".join(report.provider_status) or "-"
        lines.append(f"  {report.harness:<12} {prefix + report.status:<18} {configured}")
    return "\n".join(lines)


_RC_STATUS_LABELS = {
    "created": "created",
    "appended": "modified (source block appended)",
    "no-change": "no-change (source block already present)",
    "would-create": "would create",
    "would-append": "would modify (append source block)",
}

_ENV_STATUS_LABELS = {
    "created": "created",
    "updated": "modified",
    "no-change": "no-change",
    "would-create": "would create",
    "would-update": "would modify",
}


def env_status_label(status: str) -> str:
    """Human-readable status for the managed env file (created/modified/…)."""
    return _ENV_STATUS_LABELS.get(status, status)


def rc_status_label(status: str) -> str:
    """Human-readable status for an rc file (created/modified in the home base)."""
    return _RC_STATUS_LABELS.get(status, status)


def print_key_sources(selections: Sequence[Tuple[ProviderProfile, str]]) -> str:
    """Human-readable, masked list of which env vars carry which keys."""
    out = []
    for profile, key in selections:
        out.append(f"  {profile.api_key_env}={mask_key(key)} ({profile.name})")
    return "\n".join(out)
