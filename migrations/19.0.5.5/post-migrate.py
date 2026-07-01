# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# A one-time data fix that runs automatically when the module is upgraded to
# this version (see the folder name). The docstring below says what it fixes.
# === AI AGENT ===
# Odoo migration: def migrate(cr, version). pre-/post- by filename. Runs only
# when upgrading ACROSS this version. Idempotent-safe. Uses raw cr or a sudo env.
# ============================================================================
"""19.0.5.5 — reopen timecards wrongly auto-closed at the period boundary.

Before 19.0.5.5, _elks_close_old archived every non-approved card whose
period ended before the CURRENT period started. At a period/month rollover
(and with a UTC server ahead of the lodge's local time) this closed the
just-completed period the instant the calendar ticked over — so approvers
lost the very period they needed to validate. The logic now keeps a full
period of grace; this backfill re-opens the recent, non-approved cards that
were closed by the old rule so they reappear on the approvals page.
"""
import logging
from datetime import timedelta

from odoo import api, fields, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Timecard = env["elks.timecard"]
    Cron = env["elksattendance.timecard.cron"]

    freq = Timecard._frequency()
    today = fields.Date.context_today(Timecard)
    cur_start, _cur_end = Cron._get_current_period(today, freq)
    prev_start, _prev_end = Cron._get_current_period(
        cur_start - timedelta(days=1), freq)

    # Reopen closed, non-approved cards ending on/after the previous period's
    # start (i.e. the current + just-completed periods) — exactly the ones the
    # old rule could have wrongly hidden.
    wrong = Timecard.sudo().search([
        ("x_closed", "=", True),
        ("state", "!=", "approved"),
        ("period_end", ">=", prev_start),
    ])
    if wrong:
        wrong.write({"x_closed": False})
        _logger.info(
            "elksattendance 19.0.5.5: reopened %d wrongly-closed timecard(s).",
            len(wrong))

    # Turn OFF the daily "Close Old Pay Periods" cron. The one-time backlog is
    # handled by the 19.0.5.0 migration and the approver page already defaults
    # to the current period, so the nightly job isn't needed — and it was what
    # archived the just-ended period at the timezone rollover. The cron record
    # is noupdate=1, so the XML change to active=False won't touch the existing
    # one; deactivate it explicitly here. (Re-enable in Settings -> Technical ->
    # Scheduled Actions if ongoing auto-archiving is ever wanted; it's now safe.)
    cron = env.ref("elksattendance.ir_cron_close_old_periods",
                   raise_if_not_found=False)
    if cron and cron.active:
        cron.active = False
        _logger.info("elksattendance 19.0.5.5: deactivated Close Old Pay Periods cron.")
