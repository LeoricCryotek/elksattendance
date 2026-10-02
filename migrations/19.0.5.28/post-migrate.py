# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# A one-time data fix that runs automatically when the module is upgraded to
# this version (see the folder name). The docstring below says what it fixes.
# === AI AGENT ===
# Odoo migration: def migrate(cr, version). pre-/post- by filename. Runs only
# when upgrading ACROSS this version. Idempotent-safe. Uses raw cr or a sudo env.
# ============================================================================
"""19.0.5.28 — remove the duplicate "Payroll Timecards" reporting menu.

The module defines exactly one "Payroll Timecards" menu
(elksattendance.menu_attendance_timecard_report). A second one showed up in
Attendances -> Reporting from a stale/orphaned menu record left behind by an
earlier version. This deletes any extra "Payroll Timecards" menu pointing at
the timecard-report wizard action, keeping only the canonical one.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    action = env.ref('elksattendance.action_elks_timecard_report_wizard',
                     raise_if_not_found=False)
    if not action:
        return
    keep = env.ref('elksattendance.menu_attendance_timecard_report',
                   raise_if_not_found=False)
    dups = env['ir.ui.menu'].search([
        ('name', '=', 'Payroll Timecards'),
        ('action', '=', 'ir.actions.act_window,%d' % action.id),
    ])
    to_remove = dups - (keep or env['ir.ui.menu'])
    if to_remove:
        _logger.info("elksattendance 19.0.5.28: removing %d duplicate "
                     "'Payroll Timecards' menu(s).", len(to_remove))
        to_remove.unlink()
