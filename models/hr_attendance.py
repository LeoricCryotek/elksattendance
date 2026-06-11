# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# Lodge-specific tweaks to Odoo's clock in/out records. Three things: (1) paid
# time is the raw clock-in to clock-out span with NO automatic lunch/break
# deduction; (2) tip tracking per shift; (3) the glue that drives timecard
# emails, the "approved period is locked" rule, and reopening a card when its
# time changes.
#
# === AI AGENT ===
# Inherits hr.attendance (already a mail.thread in this build). create/write/
# unlink are overridden to: queue post-shift emails, reopen affected timecards
# (snapshot of (employee, check_in)), and BLOCK edits/deletes on approved
# periods (context 'elks_bypass_lock' skips the lock). _compute_worked_hours
# OVERRIDES core (narrowed depends to check_in/check_out) — do not re-add break
# logic. x_timecard_state is a non-stored compute used to hide the backend
# Approve button on locked rows.
# =============================================================================
"""Extend hr.attendance with tip / gratuity tracking and a lodge-policy
override of ``worked_hours``.

Worked-hours policy
-------------------
Odoo core computes ``worked_hours`` as ``check_out - check_in`` and
then subtracts any unpaid breaks (lunch, etc.) defined in the
employee's resource calendar.  The lodge does NOT want that deduction:
shifts are paid for the full clock-in → clock-out span, and if someone
takes a real break they're expected to clock out and clock back in
(two separate attendance records, each reflecting actual time on the
clock).  We therefore override ``_compute_worked_hours`` to return
the raw delta with no break subtraction.

Tip tracking
------------
``x_is_tipped_shift`` is a related Boolean pulled from the employee's
``x_receives_tips`` flag.  It is stored so it can be filtered, grouped,
and reported on (e.g. in the payroll timecard report).

``x_tip_amount`` stores the actual gratuity received for that shift.
It is only editable when the employee is flagged as tipped.
"""
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# =============================================================================
# === HUMAN ===
# The attendance (shift) record with our added fields: tip flag/amount, a few
# email-bookkeeping flags, and a read-only "what state is this shift's timecard
# in" helper.
#
# === AI AGENT ===
# x_is_tipped_shift is a STORED related off the employee flag (so it's
# filterable/reportable). The x_post_shift_* and x_long_shift_alerted booleans
# are dedup flags (copy=False) read by the crons in timecard_cron.py.
# =============================================================================
class HrAttendance(models.Model):
    _inherit = "hr.attendance"

    x_is_tipped_shift = fields.Boolean(
        "Tipped Shift",
        related="employee_id.x_receives_tips",
        store=True,
        help="Automatically set from the employee's 'Receives Tips' "
             "flag.  Used to identify shifts that should include tip "
             "reporting on payroll timecards.",
    )

    x_tip_amount = fields.Float(
        "Tips / Gratuity",
        digits=(10, 2),
        default=0.0,
        help="Gratuity amount received for this shift. "
             "Only applicable when the employee is flagged as "
             "receiving tips.",
    )

    # ------------------------------------------------------------------
    # Email-automation bookkeeping flags
    # ------------------------------------------------------------------
    x_post_shift_email_pending = fields.Boolean(
        "Post-Shift Email Queued",
        default=False,
        copy=False,
        help="Set when a shift is closed: the post-clock-out timecard "
             "email is queued and sent by a cron after a short delay "
             "(so any tip entered at the kiosk is included).",
    )
    x_post_shift_email_sent = fields.Boolean(
        "Post-Shift Email Sent",
        default=False,
        copy=False,
        help="Set once the post-clock-out timecard email has been sent "
             "for this attendance, so editing a closed record does not "
             "re-send it.",
    )
    x_long_shift_alerted = fields.Boolean(
        "Long-Shift Alert Sent",
        default=False,
        copy=False,
        help="Set once a 'possibly forgot to clock out' alert has been "
             "sent for this still-open attendance, so the hourly check "
             "does not alert repeatedly.",
    )
    x_timecard_state = fields.Char(
        "Timecard Status", compute='_compute_x_timecard_state',
        help="State of the pay-period timecard that covers this shift "
             "(used to lock approved periods).")

    # === HUMAN ===
    # Looks up the approval state of the timecard this shift belongs to.
    # === AI AGENT ===
    # Non-stored; used by the form to hide the Approve button / show the
    # "locked" badge. Uses _elks_find_timecard (no create) to avoid side effects.
    @api.depends('employee_id', 'check_in', 'check_out', 'x_tip_amount')
    def _compute_x_timecard_state(self):
        for att in self:
            tc = att._elks_find_timecard()
            att.x_timecard_state = tc.state if tc else False

    # ------------------------------------------------------------------
    # ORM overrides: email queueing, timecard reopen, approved-period lock
    # === HUMAN ===
    # When shifts are created/edited/deleted: queue the post-shift email, stop
    # edits to already-approved periods, and reopen a card if its time changed.
    # === AI AGENT ===
    # _ELKS_TIMECARD_FIELDS = the time fields whose change matters. write()
    # asserts the lock BEFORE super() (so the edit is refused), then after super
    # queues emails on the open->closed transition and reopens affected cards.
    # create() does NOT lock (new entries are how you correct a locked period).
    # context 'elks_bypass_lock' bypasses the lock (none set today; reserved).
    # ------------------------------------------------------------------
    # Attendance fields whose change should reopen an already-signed
    # timecard for re-approval.
    _ELKS_TIMECARD_FIELDS = ("check_in", "check_out", "x_tip_amount", "employee_id")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._elks_queue_post_shift_email()
        records._elks_reset_affected_timecards()
        return records

    def write(self, vals):
        # Lock approved periods: editing a time field on an approved line
        # is not allowed (corrections go in as new entries).
        if set(vals) & set(self._ELKS_TIMECARD_FIELDS):
            self._elks_assert_not_locked()
        # Capture records transitioning open -> closed in THIS write, so
        # we queue only on the actual clock-out and not on later edits
        # to an already-closed record.
        newly_closing = self.browse()
        if vals.get("check_out"):
            newly_closing = self.filtered(lambda a: not a.check_out)
        res = super().write(vals)
        if newly_closing:
            newly_closing._elks_queue_post_shift_email()
        if any(f in vals for f in self._ELKS_TIMECARD_FIELDS):
            self._elks_reset_affected_timecards()
        return res

    def unlink(self):
        attendances = self.exists()
        attendances._elks_assert_not_locked()
        snapshot = [(a.employee_id, a.check_in) for a in attendances]
        res = super().unlink()
        self.env["elks.timecard"]._elks_reset_for_snapshot(snapshot)
        return res

    # === HUMAN ===
    # Tells the timecard layer "these shifts changed — reopen their period if it
    # was already signed."
    # === AI AGENT ===
    # Builds the (employee, check_in) snapshot and hands it to
    # elks.timecard._elks_reset_for_snapshot. Skips rows with no employee/check_in.
    def _elks_reset_affected_timecards(self):
        """Reopen any signed timecard whose period covers these shifts."""
        snapshot = [
            (a.employee_id, a.check_in)
            for a in self
            if a.employee_id and a.check_in
        ]
        if snapshot:
            self.env["elks.timecard"]._elks_reset_for_snapshot(snapshot)

    # ------------------------------------------------------------------
    # Timecard lookup + the approved-period lock + backend approve button
    # === HUMAN ===
    # Find the timecard for this shift (with/without creating it), refuse edits
    # to approved periods, and let a supervisor approve the period straight from
    # the shift form.
    # === AI AGENT ===
    # _elks_timecard CREATES (get_or_create); _elks_find_timecard does NOT
    # (search only) — use the latter in computes/lock checks to avoid side
    # effects. action_elks_approve_timecard calls timecard._elks_sign(...,'approver')
    # which itself permits the override case; returns an act_window to the card.
    # ------------------------------------------------------------------
    def _elks_timecard(self):
        """Get-or-create the pay-period timecard that covers this shift."""
        self.ensure_one()
        if not (self.employee_id and self.check_in):
            return self.env["elks.timecard"]
        ref = fields.Date.context_today(self, self.check_in)
        return self.env["elks.timecard"]._get_or_create(self.employee_id, ref)

    def _elks_find_timecard(self):
        """Find (do NOT create) the pay-period timecard covering this shift."""
        self.ensure_one()
        Timecard = self.env["elks.timecard"]
        if not (self.employee_id and self.check_in):
            return Timecard
        ref = fields.Date.context_today(self, self.check_in)
        start, end = self.env["elksattendance.timecard.cron"]._get_current_period(
            ref, Timecard._frequency())
        return Timecard.sudo().search([
            ("employee_id", "=", self.employee_id.id),
            ("period_start", "=", start),
            ("period_end", "=", end),
        ], limit=1)

    def _elks_assert_not_locked(self):
        """Block edits/deletes once the covering pay period is approved.

        Corrections to an approved period are made by adding a NEW
        attendance entry (which reopens the period for re-approval), not
        by editing or deleting an approved line.
        """
        if self.env.context.get("elks_bypass_lock"):
            return
        for att in self:
            tc = att._elks_find_timecard()
            if tc and tc.state == "approved":
                raise UserError(_(
                    "This pay period is already approved and locked. "
                    "To correct it, add a NEW time entry for that day "
                    "(it will go back for approval) — approved lines "
                    "can't be edited or deleted."))

    def action_elks_approve_timecard(self):
        """Supervisor approves the pay-period timecard containing this shift.

        Works as an override too (the timecard model permits approving even
        when the employee hasn't signed).
        """
        self.ensure_one()
        tc = self._elks_timecard()
        if not tc:
            return False
        tc._elks_sign(self.env.user, 'approver')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Timecard"),
            'res_model': 'elks.timecard',
            'res_id': tc.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # === HUMAN ===
    # On clock-out, just flags the shift as "email this soon" — the actual email
    # goes out a few minutes later so a tip entered right after is included.
    # === AI AGENT ===
    # Deliberately does NO render/send (kiosk clock-out must stay instant and
    # never fail on mail). Only flips x_post_shift_email_pending. The 5-min cron
    # _cron_send_post_shift_emails does the work. Dedup via *_sent / *_pending.
    def _elks_queue_post_shift_email(self):
        """Mark just-closed shifts as awaiting their post-shift email.

        We only flip a flag here — no PDF render, no mail send — so the
        kiosk clock-out stays instant and can never be blocked by a mail
        problem. ``elksattendance.timecard.cron._cron_send_post_shift_emails``
        sends the email once the configured delay has elapsed.
        """
        to_queue = self.filtered(
            lambda a: a.check_out
            and not a.x_post_shift_email_sent
            and not a.x_post_shift_email_pending
        )
        if to_queue:
            to_queue.write({"x_post_shift_email_pending": True})

    # ------------------------------------------------------------------
    # Lodge policy: worked_hours = raw clock-in → clock-out duration,
    # never adjusted for breaks / lunch defined in the resource calendar.
    # === HUMAN ===
    # Paid time = clock-out minus clock-in, full stop. No automatic lunch/break
    # subtraction; real breaks mean clocking out and back in.
    # === AI AGENT ===
    # OVERRIDES core _compute_worked_hours with a NARROWED depends (check_in,
    # check_out only). Do not widen it back to include resource calendar / break
    # fields or the deduction returns. Open shifts report 0.
    # ------------------------------------------------------------------
    @api.depends("check_in", "check_out")
    def _compute_worked_hours(self):
        """Override Odoo core to skip break / lunch deduction.

        Core's compute subtracts any unpaid break declared in the
        employee's resource calendar (e.g. a 12:00–13:00 lunch).  The
        lodge wants Worked Time to equal raw clock-in → clock-out: if
        a volunteer or employee takes a real break, they must clock
        out and clock back in.  Two attendance records, each reflecting
        time actually on the clock.
        """
        for attendance in self:
            if attendance.check_in and attendance.check_out:
                delta = attendance.check_out - attendance.check_in
                attendance.worked_hours = delta.total_seconds() / 3600.0
            else:
                attendance.worked_hours = 0.0
