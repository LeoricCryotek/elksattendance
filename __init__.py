# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# Loads the module's Python sub-packages.
# === AI AGENT ===
# Imports models, controllers, wizard (order doesn't matter here).
# ============================================================================
from . import controllers
from . import models
from . import wizard


def alphabetize_app_menus(env):
    """Sort every root app-launcher menu alphabetically by name.
    Mirrors the elkssecretary Alphabetize App Menus tool.  Runs on
    install (post_init_hook) and on upgrade (post-migrate). Idempotent."""
    root_menus = env["ir.ui.menu"].sudo().search(
        [("parent_id", "=", False)],
        order="name asc",
    )
    for i, menu in enumerate(root_menus, start=1):
        if menu.sequence != i * 10:
            menu.sequence = i * 10


def _post_init_hook(env):
    alphabetize_app_menus(env)
