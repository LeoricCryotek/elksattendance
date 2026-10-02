# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# A one-time data fix that runs automatically when the module is upgraded to
# this version (see the folder name). The docstring below says what it fixes.
# === AI AGENT ===
# Odoo migration: def migrate(cr, version). pre-/post- by filename. Runs only
# when upgrading ACROSS this version. Idempotent-safe. Uses raw cr or a sudo env.
# ============================================================================
"""19.0.5.30 — remove the LEFTOVER duplicate "Payroll Timecards" menu.

The 19.0.5.28 migration tried to delete the duplicate but matched on the
wizard action id. The orphan menu points at a STALE/old action record (left
over from an earlier module version), so that filter never matched it and the
duplicate survived. This version matches by NAME + PARENT instead, so it
catches the orphan regardless of which action it dangles from. We keep only
the canonical menu (elksattendance.menu_attendance_timecard_report) under
Attendances -> Reporting; the separately-named Elks Charity entry is untouched.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    reporting = env.ref('hr_attendance.menu_hr_attendance_reporting',
                        raise_if_not_found=False)
    keep = env.ref('elksattendance.menu_attendance_timecard_report',
                   raise_if_not_found=False)
    if not reporting:
        return
    # Every menu named "Payroll Timecards" sitting directly under the
    # Attendances -> Reporting menu, except the one canonical record.
    candidates = env['ir.ui.menu'].with_context(active_test=False).search([
        ('name', '=', 'Payroll Timecards'),
        ('parent_id', '=', reporting.id),
    ])
    to_remove = candidates - (keep or env['ir.ui.menu'])
    if to_remove:
        _logger.info("elksattendance 19.0.5.30: removing %d leftover duplicate "
                     "'Payroll Timecards' menu(s): %s",
                     len(to_remove), to_remove.ids)
        to_remove.unlink()
