"""Route blueprints, one per area. create_app in essl_hub.web registers them."""

from __future__ import annotations

from . import admin, api, fix_review, fixes, me, people, signin, team

# fix_review adds the HR review routes to fixes.bp: importing it here is what
# registers them, once, before create_app registers the blueprint.
__all__ = ["BLUEPRINTS", "admin", "api", "fix_review", "fixes", "me", "people", "signin", "team"]

BLUEPRINTS = (signin.bp, me.bp, team.bp, people.bp, admin.bp, api.bp, fixes.bp)
