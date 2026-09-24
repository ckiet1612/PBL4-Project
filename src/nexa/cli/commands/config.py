from __future__ import annotations

import os
from typing import Annotated

import typer
from typer import Context

from ..config import ConfigStore, Profile, resolve_context
from ..errors import CliError, OutputMode, render_error
from ..output import emit_success


def _state(ctx: Context) -> dict:
    return ctx.find_root().obj or {}


def _store() -> ConfigStore:
    return ConfigStore()


def _context(ctx: Context):
    state = _state(ctx)
    store = _store()
    return store, resolve_context(
        store.load(),
        endpoint=state.get("endpoint"),
        profile=state.get("profile"),
        tenant_id=state.get("tenant_id"),
        environ=state.get("environ", os.environ),
    )


def _finish_error(exc: CliError, ctx: Context) -> None:
    render_error(exc, _state(ctx).get("output", OutputMode.HUMAN))
    raise typer.Exit(code=exc.exit_code)


def register(group: typer.Typer) -> None:
    @group.command("show")
    def show(ctx: Context) -> None:
        """Show the resolved local context with credentials redacted."""
        try:
            store, resolved = _context(ctx)
            payload = {
                "profile": resolved.profile,
                "endpoint": resolved.endpoint,
                "tenant_id": resolved.tenant_id,
                "token": "<redacted>" if store.get_token(resolved.profile) else None,
            }
            emit_success(payload, mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _finish_error(exc, ctx)

    @group.command("set-endpoint")
    def set_endpoint(ctx: Context, endpoint: Annotated[str, typer.Argument()]) -> None:
        """Persist an endpoint for the selected profile."""
        try:
            store, resolved = _context(ctx)
            loaded = store.load()
            profiles = dict(loaded.profiles)
            old = profiles.get(resolved.profile, Profile())
            profiles[resolved.profile] = Profile(endpoint=endpoint, tenant_id=old.tenant_id)
            store.save(type(loaded)(profiles=profiles, active_profile=loaded.active_profile))
            emit_success(
                {"profile": resolved.profile, "endpoint": endpoint},
                mode=_state(ctx).get("output", OutputMode.HUMAN),
            )
        except CliError as exc:
            _finish_error(exc, ctx)

    @group.command("set-tenant")
    def set_tenant(ctx: Context, tenant_id: Annotated[str, typer.Argument()]) -> None:
        """Persist a tenant context for the selected profile."""
        try:
            store, resolved = _context(ctx)
            loaded = store.load()
            profiles = dict(loaded.profiles)
            old = profiles.get(resolved.profile, Profile())
            profiles[resolved.profile] = Profile(endpoint=old.endpoint, tenant_id=tenant_id)
            store.save(type(loaded)(profiles=profiles, active_profile=loaded.active_profile))
            emit_success(
                {"profile": resolved.profile, "tenant_id": tenant_id},
                mode=_state(ctx).get("output", OutputMode.HUMAN),
            )
        except CliError as exc:
            _finish_error(exc, ctx)

    @group.command("use-profile")
    def use_profile(ctx: Context, profile: Annotated[str, typer.Argument()]) -> None:
        """Select the active local profile."""
        try:
            store = _store()
            loaded = store.load()
            profiles = dict(loaded.profiles)
            profiles.setdefault(profile, Profile())
            store.save(type(loaded)(profiles=profiles, active_profile=profile))
            emit_success({"profile": profile}, mode=_state(ctx).get("output", OutputMode.HUMAN))
        except CliError as exc:
            _finish_error(exc, ctx)
