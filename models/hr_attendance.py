# -*- coding: utf-8 -*-
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
from odoo import api, fields, models


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
    # Lodge policy: worked_hours = raw clock-in → clock-out duration,
    # never adjusted for breaks / lunch defined in the resource calendar.
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
