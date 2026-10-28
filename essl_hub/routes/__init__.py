"""Route blueprints, one per area. create_app in essl_hub.web registers them."""

from __future__ import annotations

from . import admin, api, fix_direct, fix_review, fixes, me, people, signin, team

# fix_review and fix_direct add the HR review and admin direct-fix routes to
# fixes.bp: importing them here is what registers them, once, before
# create_app registers the blueprint.
__all__ = ["BLUEPRINTS", "admin", "api", "fix_direct", "fix_review", "fixes", "me", "people", "signin", "team"]

BLUEPRINTS = (signin.bp, me.bp, team.bp, people.bp, admin.bp, api.bp, fixes.bp)
