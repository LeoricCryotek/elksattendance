# -*- coding: utf-8 -*-
"""19.0.1.4 — Recompute worked_hours on every existing attendance.

Up to 19.0.1.3 we used Odoo core's worked_hours computation, which
subtracts unpaid breaks (lunch, etc.) defined in the employee's
resource calendar.  The lodge policy is now: worked_hours = raw
clock-in → clock-out duration with NO break deduction.

This migration:
    1. Logs how many records are about to be recomputed.
    2. Forces the new _compute_worked_hours method to run on every
       hr.attendance row that already has both check_in and check_out.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        SELECT COUNT(*) FROM hr_attendance
        WHERE check_in IS NOT NULL AND check_out IS NOT NULL
    """)
    n = cr.fetchone()[0]
    _logger.info(
        "elksattendance 19.0.1.4 migration: recomputing worked_hours "
        "on %d closed attendance records (removes any resource-calendar "
        "break deduction)...",
        n,
    )

    # Recompute in-place via raw SQL.  Faster than triggering the ORM
    # for thousands of rows, and matches the new _compute_worked_hours
    # formula exactly: raw delta in hours.
    cr.execute("""
        UPDATE hr_attendance
        SET worked_hours = EXTRACT(EPOCH FROM (check_out - check_in)) / 3600.0
        WHERE check_in IS NOT NULL AND check_out IS NOT NULL
    """)

    # Open shifts (no check_out yet) should report 0 hours — match
    # the new compute's else branch.
    cr.execute("""
        UPDATE hr_attendance
        SET worked_hours = 0
        WHERE check_in IS NOT NULL AND check_out IS NULL
    """)

    _logger.info("elksattendance 19.0.1.4 migration: done.")
