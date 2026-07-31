# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# Re-alphabetizes the app-launcher grid on every upgrade — same
# behavior as the elkssecretary "Alphabetize App Menus" tool but
# automated.
# === AI AGENT ===
# Delegates to alphabetize_app_menus() in the module's __init__.py.
# Idempotent — safe to run on any subsequent upgrade too.
# ============================================================================
"""19.0.5.9 — Auto-alphabetize app launcher on upgrade."""


def migrate(cr, version):
    if not version:
        return
    from odoo import api, SUPERUSER_ID
    from odoo.addons.elksattendance import alphabetize_app_menus
    env = api.Environment(cr, SUPERUSER_ID, {})
    alphabetize_app_menus(env)
