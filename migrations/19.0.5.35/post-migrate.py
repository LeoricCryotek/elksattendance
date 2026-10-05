# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# A one-time data fix that runs automatically when the module is upgraded to
# this version (see the folder name). The docstring below says what it fixes.
# === AI AGENT ===
# Odoo migration: def migrate(cr, version). pre-/post- by filename. Runs only
# when upgrading ACROSS this version. Idempotent-safe. Uses raw cr or a sudo env.
# ============================================================================
"""19.0.5.35 — remove stale legacy-boundary duplicate timecards.

When the semi-monthly split changed from 1st-15th / 16th-EOM to 1st-14th /
15th-EOM, timecards already created under the OLD boundary (e.g. 10/01-10/15)
stuck around alongside the new correct ones (10/01-10/14), so some employees
showed TWO cards for the same stretch.

This deletes any timecard whose (period_start, period_end) does NOT match the
period the current logic computes for that start date — but ONLY when the card
is not yet approved AND its period ends today or later. That protects every
previously approved/paid period (and anything that already ended), honoring
"previous pay periods must not be altered." The attendances themselves are
untouched; the correct card is (re)created on demand.
"""
import logging

from odoo import api, fields, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Timecard = env['elks.timecard']
    Cron = env['elksattendance.timecard.cron']
    freq = Cron._frequency()
    today = fields.Date.context_today(Cron)

    # Only current/future, not-yet-approved cards are candidates.
    candidates = Timecard.search([
        ('period_end', '>=', today),
        ('state', '!=', 'approved'),
    ])
    stale = Timecard.browse()
    for tc in candidates:
        correct_start, correct_end = Cron._get_current_period(
            tc.period_start, freq)
        if (tc.period_start, tc.period_end) != (correct_start, correct_end):
            stale |= tc

    if stale:
        _logger.info(
            "elksattendance 19.0.5.35: removing %d stale wrong-boundary "
            "timecard(s): %s", len(stale),
            [(t.employee_id.name, str(t.period_start), str(t.period_end))
             for t in stale])
        stale.unlink()
