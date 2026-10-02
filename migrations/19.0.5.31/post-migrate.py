# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# A one-time data fix that runs automatically when the module is upgraded to
# this version (see the folder name). The docstring below says what it fixes.
# === AI AGENT ===
# Odoo migration: def migrate(cr, version). pre-/post- by filename. Runs only
# when upgrading ACROSS this version. Idempotent-safe. Uses raw cr or a sudo env.
# ============================================================================
"""19.0.5.31 — CATCH-ALL removal of the duplicate "Payroll Timecards" menu.

Two earlier migrations tried and missed this orphan:
  * 19.0.5.28 matched on the wizard ACTION id — but the orphan dangles off a
    stale/old action record, so it never matched.
  * 19.0.5.30 matched name + parent via the ORM — but ir.ui.menu.name is a
    translated jsonb column, and the migration only runs when the upgrade
    actually CROSSES .30 (if .30 was already installed it never re-ran).

This version runs at the new target version and matches purely by NAME in raw
SQL (any translation value equal to "Payroll Timecards"), independent of parent,
action, or which language the label is stored under. It keeps only the module's
two canonical menu records and unlinks everything else with that name.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# The menus this module legitimately owns with (or related to) that label.
_KEEP_XMLIDS = (
    'elksattendance.menu_attendance_timecard_report',
    'elksattendance.menu_elkscharity_timecard_report',
)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    keep_ids = set()
    for xid in _KEEP_XMLIDS:
        rec = env.ref(xid, raise_if_not_found=False)
        if rec:
            keep_ids.add(rec.id)

    # Any ir.ui.menu whose name — in ANY stored translation — is exactly
    # "Payroll Timecards". jsonb column, so iterate its values in SQL.
    cr.execute("""
        SELECT id
          FROM ir_ui_menu
         WHERE EXISTS (
               SELECT 1
                 FROM jsonb_each_text(name) AS kv
                WHERE kv.value = 'Payroll Timecards'
         )
    """)
    found_ids = {row[0] for row in cr.fetchall()}
    to_remove = env['ir.ui.menu'].browse(found_ids - keep_ids)
    if to_remove:
        _logger.info("elksattendance 19.0.5.31: removing %d duplicate "
                     "'Payroll Timecards' menu(s): %s",
                     len(to_remove), to_remove.ids)
        to_remove.unlink()
