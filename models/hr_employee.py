# -*- coding: utf-8 -*-
"""Extend hr.employee with attendance-related fields.

``x_manager_user_ids`` stores all ``res.users`` IDs that sit above this
employee in the org tree (via ``parent_id``).  The field is recomputed
whenever a manager assignment changes, cascading to all subordinates.

The record rule in ``elksattendance_security.xml`` uses this field so
that any manager in the chain gets attendance access.

``x_receives_tips`` is a simple Boolean flag set by a manager to
indicate that this employee receives tips or gratuity.  When True,
every attendance record for this employee is auto-flagged via the
related field ``x_is_tipped_shift`` on ``hr.attendance``.
"""
from odoo import api, fields, models


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    x_receives_tips = fields.Boolean(
        "Receives Tips / Gratuity",
        default=False,
        tracking=True,
        help="Check this box if the employee receives tips or gratuity. "
             "Their attendance records will be automatically flagged "
             "for tip tracking on payroll timecards.",
    )

    x_manager_user_ids = fields.Many2many(
        "res.users",
        "hr_employee_manager_user_rel",
        "employee_id",
        "user_id",
        string="Manager Chain (Users)",
        compute="_compute_manager_user_ids",
        store=True,
        recursive=True,
        help="All res.users linked to managers above this employee in "
             "the org tree.  Used by attendance record rules.",
    )

    @api.depends('parent_id', 'parent_id.user_id', 'parent_id.x_manager_user_ids')
    def _compute_manager_user_ids(self):
        """Walk up the org tree and collect every manager's user_id.

        Because the depends includes ``parent_id.x_manager_user_ids``,
        changing a manager high in the tree cascades automatically to
        all employees below — Odoo's ORM triggers recompute on every
        record whose dependency changed.
        """
        for emp in self:
            user_ids = set()
            manager = emp.parent_id
            if manager and manager.user_id:
                user_ids.add(manager.user_id.id)
                # Add the parent's already-computed chain
                user_ids |= set(manager.x_manager_user_ids.ids)
            emp.x_manager_user_ids = [(6, 0, list(user_ids))]
