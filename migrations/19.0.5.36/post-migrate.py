# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# Re-runs the duplicate-timecard cleanup. The 19.0.5.35 attempt errored (it
# called the pay-frequency helper on the wrong model), so the duplicates were
# never removed. This runs the corrected, idempotent cleanup.
# === AI AGENT ===
# Odoo migration: def migrate(cr, version). Delegates to the idempotent model
# method elks.timecard._elks_prune_wrong_boundary_cards().
# ============================================================================
"""19.0.5.36 — re-run duplicate-timecard cleanup (fixes the broken 5.35 run)."""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['elks.timecard']._elks_prune_wrong_boundary_cards()
