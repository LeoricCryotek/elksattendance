# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# A one-time data fix that runs automatically when the module is upgraded to
# this version (see the folder name). The docstring below says what it fixes.
# === AI AGENT ===
# Odoo migration: def migrate(cr, version). pre-/post- by filename. Runs only
# when upgrading ACROSS this version. Idempotent-safe. Uses raw cr or a sudo env.
# ============================================================================
"""19.0.4.2 — recompute elks.timecard.approver_id.

Up to 19.0.4.1 the stored ``approver_id`` only recomputed on
employee/period change, so timecards created before the employee's
Attendance approver was set kept an empty approver — and never showed
up on that approver's portal Approvals page. The dependency now includes
``employee_id.attendance_manager_id``; this backfills existing rows.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    timecards = env['elks.timecard'].search([])
    if not timecards:
        return
    env.add_to_compute(
        env['elks.timecard']._fields['approver_id'], timecards)
    env.flush_all()
    _logger.info(
        "elksattendance 19.0.4.2: recomputed approver_id on %d timecard(s).",
        len(timecards))
