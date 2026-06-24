# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# This is the timecard itself: one record per employee per pay period. It pulls
# together that period's shifts, tracks who signed off (employee, then
# supervisor), and is what the employee/approver see in the portal. If time in
# an already-approved period changes, the signatures are wiped so it must be
# re-approved — nobody gets paid on numbers that changed after sign-off.
#
# === AI AGENT ===
# Model: elks.timecard. Inherits portal.mixin (tokenless /my/timecard/<id>) +
# mail.thread (chatter/tracking). attendance_ids/totals are NON-stored computes
# that re-search the payroll domain live, so they always reflect current
# attendance. approver_id is STORED (searchable) = employee.attendance_manager_id.
# State: draft -> employee_approved -> approved. Volunteer-dept employees are
# excluded everywhere (no card). Most cross-calls come from hr_attendance.py
# (reset hooks), timecard_cron.py (emails), and controllers/portal.py.
# =============================================================================
"""Per-employee, per-pay-period timecard with two-step approval.

An ``elks.timecard`` aggregates one employee's payroll shifts for a pay
period and carries the approval state + signatures:

    draft -> employee_approved -> approved

Signatures are stamped with the signing user's name and a timestamp.
The approver is the employee's **Attendance** approver
(``attendance_manager_id``).  If attendance in an already-signed period
is added or edited, the signatures auto-reset to draft (see the hooks
in ``hr_attendance.py``) so nobody is "paid based on" a card that
changed after they approved it.

The model uses ``portal.mixin`` so each record has a tokenised
``/my/timecard/<id>`` URL employees can open from the portal or an
email link.
"""
import logging
from datetime import date

from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError

_logger = logging.getLogger(__name__)

PARAM_PREFIX = 'elksattendance.'


# =============================================================================
# === HUMAN ===
# The timecard record and all its fields: which employee/period, the shifts and
# totals, the approval status, and the two signature blocks.
#
# === AI AGENT ===
# _rec_name=display_name (we override display_name as a stored compute).
# Unique constraint on (employee, period_start, period_end) via models.Constraint
# (Odoo 19 API, NOT _sql_constraints). copy=False on signatures so duplicating a
# card doesn't carry sign-offs.
# =============================================================================
class ElksTimecard(models.Model):
    _name = 'elks.timecard'
    _description = 'Employee Timecard (Pay Period)'
    _inherit = ['portal.mixin', 'mail.thread']
    _order = 'period_start desc, employee_id'
    _rec_name = 'display_name'

    employee_id = fields.Many2one(
        'hr.employee', string="Employee", required=True, index=True,
        ondelete='cascade', tracking=True)
    company_id = fields.Many2one(
        'res.company', related='employee_id.company_id', store=True)

    period_start = fields.Date("Period Start", required=True, index=True)
    period_end = fields.Date("Period End", required=True, index=True)

    display_name = fields.Char(compute='_compute_display_name', store=True)

    attendance_ids = fields.Many2many(
        'hr.attendance', compute='_compute_attendances',
        string="Shifts")
    shift_count = fields.Integer(compute='_compute_attendances')
    total_hours = fields.Float(compute='_compute_attendances')
    total_tips = fields.Float(compute='_compute_attendances')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('employee_approved', 'Employee Approved'),
        ('approved', 'Approved'),
    ], string="Status", default='draft', required=True, tracking=True)

    approver_id = fields.Many2one(
        'res.users', string="Attendance Approver",
        compute='_compute_approver_id', store=True,
        help="The employee's Attendance approver, who gives the final "
             "sign-off.")

    # === HUMAN ===
    # "Closed" archives an old period so it stops showing on the approver's
    # to-do list and the employee's history. Closing never requires approval
    # and never sends email.
    # === AI AGENT ===
    # x_closed is the archive flag (copy=False). Portal domains filter it out;
    # the reminder cron skips it. Set in bulk by _elks_close_old (cron + the
    # 19.0.5.0 migration), or per-card via action_close / action_reopen.
    x_closed = fields.Boolean("Closed", default=False, copy=False, index=True)

    adjustment_ids = fields.One2many(
        'elks.timecard.adjustment', 'timecard_id', string="Adjustment Requests")
    pending_adjustment_count = fields.Integer(
        compute='_compute_pending_adjustments')

    # === HUMAN ===
    # How many change requests on this card are still waiting for a decision.
    # === AI AGENT ===
    # Drives the badge on the approver portal list. 'suggested' == not yet
    # applied/rejected.
    @api.depends('adjustment_ids.state')
    def _compute_pending_adjustments(self):
        for tc in self:
            tc.pending_adjustment_count = len(
                tc.adjustment_ids.filtered(lambda a: a.state == 'suggested'))

    # Signatures
    employee_signed_by = fields.Many2one('res.users', "Employee Signed By",
                                         readonly=True, copy=False)
    employee_signed_name = fields.Char("Employee Signature", readonly=True,
                                       copy=False)
    employee_signed_date = fields.Datetime("Employee Signed On",
                                           readonly=True, copy=False)
    approver_signed_by = fields.Many2one('res.users', "Approver Signed By",
                                         readonly=True, copy=False)
    approver_signed_name = fields.Char("Approver Signature", readonly=True,
                                       copy=False)
    approver_signed_date = fields.Datetime("Approver Signed On",
                                           readonly=True, copy=False)

    _uniq_employee_period = models.Constraint(
        'unique(employee_id, period_start, period_end)',
        "A timecard already exists for this employee and pay period.",
    )

    # ------------------------------------------------------------------
    # Computes
    # === HUMAN ===
    # The display label, who the approver is, and the live roll-up of shifts/
    # hours/tips for the period.
    # === AI AGENT ===
    # _compute_approver_id depends on employee_id.attendance_manager_id (added
    # 19.0.4.2) so changing the approver re-stamps existing cards; a migration
    # backfilled old rows. _compute_attendances is NON-stored on purpose so it
    # always reflects current punches; it reuses cron._payroll_domain (Volunteer
    # + charity exclusions). tips only summed when employee receives tips.
    # ------------------------------------------------------------------
    @api.depends('employee_id', 'period_start', 'period_end')
    def _compute_display_name(self):
        for tc in self:
            if tc.employee_id and tc.period_start and tc.period_end:
                tc.display_name = "%s — %s to %s" % (
                    tc.employee_id.name,
                    tc.period_start.strftime('%m/%d/%Y'),
                    tc.period_end.strftime('%m/%d/%Y'))
            else:
                tc.display_name = _("Timecard")

    @api.depends('employee_id', 'employee_id.attendance_manager_id')
    def _compute_approver_id(self):
        for tc in self:
            emp = tc.employee_id
            tc.approver_id = (
                emp.attendance_manager_id
                if emp and 'attendance_manager_id' in emp._fields
                else False)

    @api.depends('employee_id', 'period_start', 'period_end')
    def _compute_attendances(self):
        Att = self.env['hr.attendance']
        Cron = self.env['elksattendance.timecard.cron']
        for tc in self:
            if tc.employee_id and tc.period_start and tc.period_end:
                atts = Att.search(
                    Cron._payroll_domain(
                        tc.period_start, tc.period_end, tc.employee_id),
                    order='check_in')
            else:
                atts = Att.browse()
            tc.attendance_ids = atts
            tc.shift_count = len(atts)
            tc.total_hours = sum(atts.mapped('worked_hours'))
            tc.total_tips = (
                sum(atts.mapped('x_tip_amount'))
                if tc.employee_id.x_receives_tips else 0.0)

    # ------------------------------------------------------------------
    # Portal URL
    # === HUMAN ===
    # The web address of this timecard in the member portal.
    # === AI AGENT ===
    # _compute_access_url overrides portal.mixin's '#'. _elks_portal_url is the
    # absolute link put in emails — deliberately TOKENLESS (security fix
    # 19.0.4.6): the page is auth='user' and identity-checked server-side.
    # ------------------------------------------------------------------
    def _compute_access_url(self):
        super()._compute_access_url()
        for tc in self:
            tc.access_url = '/my/timecard/%s' % tc.id

    def _elks_portal_url(self):
        """Absolute portal URL for emails. Login + identity required to open.

        No access token: the page forces a login and only the employee,
        their approver, or an attendance admin may view it.
        """
        self.ensure_one()
        return "%s/my/timecard/%s" % (self.get_base_url(), self.id)

    # ------------------------------------------------------------------
    # Factory / lookup helpers
    # === HUMAN ===
    # Finding or creating the right timecard for an employee + date, deciding
    # who counts as a volunteer (no card), and bulk-ensuring cards exist for a
    # person or for everyone an approver oversees.
    # === AI AGENT ===
    # _get_or_create is the single creation choke point — it refuses Volunteer
    # employees and computes the period from the configured frequency. All
    # creation goes through sudo (portal users have no create ACL). Ensure-*
    # methods are called from the portal controllers on page load to lazily
    # backfill cards (idempotent).
    # ------------------------------------------------------------------
    @api.model
    def _frequency(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            PARAM_PREFIX + 'email_frequency', 'semi_monthly')

    @api.model
    def _elks_is_volunteer(self, employee):
        """Volunteer employees are never payroll — no timecard/approval."""
        return bool(employee.department_id
                    and employee.department_id.name == 'Volunteers')

    @api.model
    def _get_or_create(self, employee, ref_date=None):
        """Find or create the timecard for the period containing ref_date.

        Volunteer-department employees get no timecard at all — their
        hours are charity/volunteer time, not payroll, and never need
        approval.
        """
        if not employee or self._elks_is_volunteer(employee):
            return self.browse()
        ref_date = ref_date or fields.Date.context_today(self)
        start, end = self.env['elksattendance.timecard.cron']._get_current_period(
            ref_date, self._frequency())
        tc = self.sudo().search([
            ('employee_id', '=', employee.id),
            ('period_start', '=', start),
            ('period_end', '=', end),
        ], limit=1)
        if not tc:
            tc = self.sudo().create({
                'employee_id': employee.id,
                'period_start': start,
                'period_end': end,
            })
        return tc

    @api.model
    def _elks_employees_for_user(self, user):
        """Employees linked to a (portal) user, by related user or contact."""
        # === AI AGENT ===
        # Matches on user_id OR work_contact_id so ONE person holding several
        # employee records (Board, Kitchen, Bar — sharing one Work Contact) is
        # all found under a single portal login. Odoo forbids one user_id on
        # multiple employees, hence the work_contact_id branch.
        return self.env['hr.employee'].sudo().search([
            '|',
            ('user_id', '=', user.id),
            ('work_contact_id', '=', user.partner_id.id),
        ])

    @api.model
    def _elks_ensure_for_employee(self, employee):
        """Create the current-period card plus one per past period with hours."""
        if self._elks_is_volunteer(employee):
            return
        Cron = self.env['elksattendance.timecard.cron']
        freq = self._frequency()
        today = fields.Date.context_today(self)
        self._get_or_create(employee, today)
        atts = self.env['hr.attendance'].sudo().search(
            Cron._payroll_domain(date(2000, 1, 1), today, employee))
        seen = set()
        for att in atts:
            d = fields.Date.context_today(self, att.check_in)
            key = Cron._get_current_period(d, freq)
            if key not in seen:
                seen.add(key)
                self._get_or_create(employee, d)

    @api.model
    def _elks_ensure_for_user(self, user):
        """Ensure timecards exist for the employee(s) this user *is*."""
        employees = self._elks_employees_for_user(user)
        for emp in employees:
            self._elks_ensure_for_employee(emp)
        return employees

    @api.model
    def _elks_ensure_for_approver(self, user):
        """Ensure timecards exist for everyone this user *approves*.

        Lets an approver see/act on a team member's card even if that
        member has never opened the portal themselves.
        """
        emps = self.env['hr.employee'].sudo().search(
            [('attendance_manager_id', '=', user.id)])
        for emp in emps:
            self._elks_ensure_for_employee(emp)
        return emps

    # ------------------------------------------------------------------
    # Identity checks + approval actions
    # === HUMAN ===
    # Who is allowed to do what: is this user the employee, the approver, or an
    # attendance admin — and the actual sign-off / reopen actions.
    # === AI AGENT ===
    # These identity helpers are the ONLY access gate for portal actions
    # (controllers call them on sudo recordsets), so keep them strict. _elks_sign
    # is sudo-safe: explicit identity check, then sudo write (portal users are
    # read-only). Approver signing from 'draft' is a recorded supervisor
    # OVERRIDE. Employee signing also emails the approver.
    # ------------------------------------------------------------------
    def _is_officer(self, user):
        return user.has_group('hr_attendance.group_hr_attendance_user')

    def _elks_is_owner_for(self, user):
        """True if `user` is this card's employee (by related user or contact)."""
        self.ensure_one()
        emp = self.employee_id
        return bool(emp and (
            emp.user_id == user
            or (user.partner_id and emp.work_contact_id == user.partner_id)))

    def _elks_is_approver_for(self, user):
        """True if `user` is this card's Attendance approver."""
        self.ensure_one()
        return bool(self.approver_id and self.approver_id == user)

    def _elks_sign(self, user, role):
        """Record a signature as ``user`` in ``role`` ('employee'/'approver').

        Sudo-safe: the identity check is explicit (so this works when
        called on a sudo recordset from the portal, where ``user`` is the
        real logged-in user), and the write runs with sudo so portal users
        — who have read-only ACL on this model — can still sign.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        stamp = now.strftime('%m/%d/%Y %I:%M %p')
        if role == 'employee':
            if not (self._elks_is_owner_for(user) or self._is_officer(user)):
                raise AccessError(_(
                    "Only the employee can approve their own timecard."))
            vals = {
                'state': 'employee_approved',
                'employee_signed_by': user.id,
                'employee_signed_name': user.name,
                'employee_signed_date': now,
            }
            msg = _("Timecard approved by employee %(name)s on %(date)s.",
                    name=user.name, date=stamp)
            self.sudo().write(vals)
            self.sudo().message_post(body=msg)
            self.sudo()._notify_approver_to_review()
            return
        else:  # approver
            if not (self._elks_is_approver_for(user) or self._is_officer(user)):
                raise AccessError(_(
                    "Only the Attendance approver can give final approval."))
            override = self.state == 'draft'  # employee hasn't signed
            vals = {
                'state': 'approved',
                'approver_signed_by': user.id,
                'approver_signed_name': user.name,
                'approver_signed_date': now,
            }
            if override:
                msg = _(
                    "Timecard given final approval by %(name)s on %(date)s "
                    "(supervisor override — employee had not approved).",
                    name=user.name, date=stamp)
            else:
                msg = _("Timecard given final approval by %(name)s on %(date)s.",
                        name=user.name, date=stamp)
        self.sudo().write(vals)
        self.sudo().message_post(body=msg)

    def action_employee_approve(self):
        """Employee signs off on their own timecard (backend button)."""
        for tc in self:
            tc._elks_sign(self.env.user, 'employee')
        return True

    def action_approver_approve(self):
        """Attendance approver gives the final sign-off (backend button)."""
        for tc in self:
            tc._elks_sign(self.env.user, 'approver')
        return True

    def action_reset(self):
        """Reopen the timecard, clearing all signatures."""
        self._elks_reset_signatures(_("Timecard reopened."))
        return True

    # ------------------------------------------------------------------
    # Closing old periods (archive — silent, no approval, no email)
    # === HUMAN ===
    # Close/reopen a single card by hand, and the bulk job that closes old
    # periods nobody needs to approve anymore.
    # === AI AGENT ===
    # _elks_close_old closes every NON-approved card whose period ended before
    # the current period starts (approved history is kept). It's intentionally
    # silent — no message_post, no email. Called by the daily cron and the
    # 19.0.5.0 migration (the "close pre-existing backlog" cleanup).
    # ------------------------------------------------------------------
    def action_close(self):
        self.write({'x_closed': True})
        return True

    def action_reopen(self):
        self.write({'x_closed': False})
        return True

    @api.model
    def _elks_close_old(self):
        """Archive old, non-approved timecards (silent: no email/chatter)."""
        today = fields.Date.context_today(self)
        start, _end = self.env['elksattendance.timecard.cron']._get_current_period(
            today, self._frequency())
        old = self.sudo().search([
            ('period_end', '<', start),
            ('state', '!=', 'approved'),
            ('x_closed', '=', False),
        ])
        if old:
            old.write({'x_closed': True})
            _logger.info("elksattendance: closed %d old timecard(s).", len(old))
        return len(old)

    # ------------------------------------------------------------------
    # Notifications, suggestions, and auto-reset
    # === HUMAN ===
    # Emails the supervisor when an employee approves, creates a change request
    # from the portal, and the logic that reopens a card when its time changes.
    # === AI AGENT ===
    # _notify_approver_to_review uses cron._elks_send_mail (configured server).
    # _elks_create_suggestion is called from the portal suggest route; it sudo-
    # creates the adjustment and logs to the line chatter. _elks_reset_for_snapshot
    # is called by hr.attendance create/write/unlink with [(employee, check_in)]
    # tuples and is what enforces "approval can't survive a time change".
    # ------------------------------------------------------------------
    def _notify_approver_to_review(self):
        """Email the Attendance approver that the employee has signed off."""
        self.ensure_one()
        approver = self.approver_id
        if not approver or not approver.email:
            return
        period = "%s – %s" % (self.period_start.strftime('%m/%d/%Y'),
                              self.period_end.strftime('%m/%d/%Y'))
        subject = _("Timecard ready for approval: %s") % self.employee_id.name
        body = (
            "<p>%s</p><p><a href='%s' style='background:#003366;color:#fff;"
            "padding:8px 14px;border-radius:4px;text-decoration:none;'>%s</a></p>"
            % (_("%(emp)s approved their timecard for %(period)s. "
                 "Please give final approval.",
                 emp=self.employee_id.name, period=period),
               self._elks_portal_url(), _("Review &amp; approve"))
        )
        self.env['elksattendance.timecard.cron']._elks_send_mail(
            [approver.email], subject, body)

    def _elks_create_suggestion(self, attendance, proposed_in, proposed_out,
                                reason, user):
        """Create an employee adjustment suggestion and log it to chatter."""
        self.ensure_one()
        adj = self.env['elks.timecard.adjustment'].sudo().create({
            'timecard_id': self.id,
            'attendance_id': attendance.id if attendance else False,
            'current_check_in': attendance.check_in if attendance else False,
            'current_check_out': attendance.check_out if attendance else False,
            'proposed_check_in': proposed_in,
            'proposed_check_out': proposed_out,
            'reason': reason,
            'requested_by': user.id,
        })
        adj._post_request_to_timecard()
        return adj

    @api.model
    def _elks_reset_for_snapshot(self, snapshot):
        """Reopen signed timecards covering each (employee, check_in) pair.

        Called from hr.attendance create/write/unlink so an approved card
        cannot stay signed after its underlying time changes.
        """
        reason = _(
            "Reopened for re-approval because an attendance record in this "
            "period was added, changed, or removed.")
        for employee, check_in in snapshot:
            if not employee or not check_in:
                continue
            ref = fields.Date.context_today(self, check_in)
            tcs = self.sudo().search([
                ('employee_id', '=', employee.id),
                ('period_start', '<=', ref),
                ('period_end', '>=', ref),
                ('state', '!=', 'draft'),
            ])
            if tcs:
                tcs._elks_reset_signatures(reason)

    # === HUMAN ===
    # Wipes the signatures and sets a card back to Draft, noting why in chatter.
    # === AI AGENT ===
    # No-op on cards already in draft. Used by action_reset and the auto-reset
    # snapshot path. Clears BOTH signature triplets.
    def _elks_reset_signatures(self, reason):
        to_reset = self.filtered(lambda t: t.state != 'draft')
        if not to_reset:
            return
        to_reset.write({
            'state': 'draft',
            'employee_signed_by': False,
            'employee_signed_name': False,
            'employee_signed_date': False,
            'approver_signed_by': False,
            'approver_signed_name': False,
            'approver_signed_date': False,
        })
        for tc in to_reset:
            tc.message_post(body=reason)
