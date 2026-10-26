"""Route blueprints, one per area. create_app in essl_hub.web registers them."""

from __future__ import annotations

from . import admin, api, fixes, me, people, signin, team

BLUEPRINTS = (signin.bp, me.bp, team.bp, people.bp, admin.bp, api.bp, fixes.bp)
