# -*- coding: utf-8 -*-
"""Extend hr.attendance with tip / gratuity tracking.

``x_is_tipped_shift`` is a related Boolean pulled from the employee's
``x_receives_tips`` flag.  It is stored so it can be filtered, grouped,
and reported on (e.g. in the payroll timecard report).

``x_tip_amount`` stores the actual gratuity received for that shift.
It is only editable when the employee is flagged as tipped.
"""
from odoo import fields, models


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
