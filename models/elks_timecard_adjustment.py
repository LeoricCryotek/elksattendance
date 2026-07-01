# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# When an employee thinks a shift's time is wrong, they don't edit their own
# punch. They file a "request" here (proposed in/out + reason). The request is
# written into the shift's chatter so everyone can see it, and the supervisor
# either Applies it (which actually changes the punch) or Rejects it.
#
# === AI AGENT ===
# Model: elks.timecard.adjustment. A suggestion record, NOT the source of
# truth. Applying mutates the linked hr.attendance (sudo write) which, via the
# hr.attendance write-hook, reopens the covering elks.timecard for re-approval.
# State machine: suggested -> applied | rejected (terminal). Chatter target is
# the attendance line when set, else the timecard (see _thread).
# =============================================================================
"""Employee-suggested timecard adjustments.

An employee can't edit their own punch.  Instead they submit a
*suggestion* (proposed check-in / check-out + reason) against a shift on
their timecard.  The request is logged to the timecard chatter (with the
resulting hour change) and the **approver applies it** — applying writes
the new times onto the real ``hr.attendance``, which in turn reopens the
period for re-approval.
"""
from markupsafe import Markup

from odoo import api, fields, models, _


# =============================================================================
# === HUMAN ===
# The adjustment-request record: who asked, what they propose, the reason, the
# resulting change in hours, and whether it's been applied or rejected.
#
# === AI AGENT ===
# employee_id is a stored related off timecard_id (used by record rules).
# hours_delta = proposed - current span; stored so it can show in lists/portal.
# No _inherit of mail.thread here — messages are posted onto the *thread*
# returned by _thread() (attendance or timecard), not onto this record.
# =============================================================================
class ElksTimecardAdjustment(models.Model):
    _name = 'elks.timecard.adjustment'
    _description = 'Timecard Adjustment Request'
    _order = 'create_date desc'

    timecard_id = fields.Many2one(
        'elks.timecard', string="Timecard", required=True,
        ondelete='cascade', index=True)
    attendance_id = fields.Many2one(
        'hr.attendance', string="Shift", ondelete='set null')
    employee_id = fields.Many2one(
        related='timecard_id.employee_id', store=True)

    current_check_in = fields.Datetime("Current Check In", readonly=True)
    current_check_out = fields.Datetime("Current Check Out", readonly=True)
    proposed_check_in = fields.Datetime("Proposed Check In")
    proposed_check_out = fields.Datetime("Proposed Check Out")
    reason = fields.Text("Reason / Note")

    state = fields.Selection([
        ('suggested', 'Requested'),
        ('applied', 'Applied'),
        ('rejected', 'Rejected'),
    ], default='suggested', required=True)

    requested_by = fields.Many2one(
        'res.users', "Requested By", default=lambda self: self.env.user)

    current_hours = fields.Float(compute='_compute_hours', store=True)
    proposed_hours = fields.Float(compute='_compute_hours', store=True)
    hours_delta = fields.Float("Change (hrs)", compute='_compute_hours',
                               store=True)

    # === HUMAN ===
    # How many hours are between a check-in and check-out (0 if either missing).
    # === AI AGENT ===
    # Pure helper; tz-agnostic (datetimes are naive UTC, delta is identical).
    @staticmethod
    def _span_hours(ci, co):
        if ci and co:
            return (co - ci).total_seconds() / 3600.0
        return 0.0

    # === HUMAN ===
    # Works out the current hours, the proposed hours, and the difference.
    # === AI AGENT ===
    # Stored compute -> these fields are searchable/groupable. hours_delta can
    # be negative (proposed shorter than current).
    @api.depends('current_check_in', 'current_check_out',
                 'proposed_check_in', 'proposed_check_out')
    def _compute_hours(self):
        for r in self:
            r.current_hours = r._span_hours(r.current_check_in, r.current_check_out)
            r.proposed_hours = r._span_hours(r.proposed_check_in, r.proposed_check_out)
            r.hours_delta = r.proposed_hours - r.current_hours

    # === HUMAN ===
    # Formats an hour change for display, e.g. "+1:15" or "-0:30".
    # === AI AGENT ===
    # Uses a Unicode minus for negatives; output is HTML/browser context only
    # (chatter + portal), never the wkhtmltopdf slip, so it won't mojibake.
    @staticmethod
    def _fmt_delta(delta):
        sign = '+' if delta >= 0 else '−'
        d = abs(delta)
        return "%s%d:%02d" % (sign, int(d), int(round((d - int(d)) * 60)))

    # === HUMAN ===
    # Decides where the conversation about this request lives — on the specific
    # shift if we know it, otherwise on the whole pay-period timecard.
    # === AI AGENT ===
    # Both targets are mail.thread; caller posts via _post_message (sudo).
    def _thread(self):
        """Where chatter lives: the attendance line if known, else the card."""
        self.ensure_one()
        return self.attendance_id or self.timecard_id

    # === HUMAN ===
    # Posts an internal audit note about this request to the line/card chatter.
    # === AI AGENT ===
    # sudo + subtype mt_note (internal log) — these are audit entries, not the
    # public Discussion thread, so they don't notify and don't show on the
    # portal (which filters internal subtypes). body MUST be Markup to render.
    def _post_message(self, body):
        """Post an internal LOG NOTE (audit trail) to the line/card chatter.

        These are system audit entries, so they go in as notes (mt_note), not
        public messages — they appear in the backend chatter and don't notify
        followers. ``body`` must be a Markup so its HTML renders (a plain str
        is shown escaped in this Odoo build).
        """
        self.ensure_one()
        self._thread().sudo().message_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_note')

    # === HUMAN ===
    # Writes the "here's my requested change" note into the shift's chatter,
    # spelling out current vs proposed times and the net hour change.
    # === AI AGENT ===
    # Name kept for back-compat (called from elks_timecard._elks_create_suggestion).
    # fmt() localizes datetimes to the record's tz for display only.
    def _post_request_to_timecard(self):
        """Log the request (with the hour change) to the line chatter."""
        for r in self:
            def fmt(dt):
                if not dt:
                    return '—'
                local = fields.Datetime.context_timestamp(r, dt)
                return local.strftime('%m/%d/%Y %I:%M %p')
            # Markup template = trusted HTML; the %-args are auto-escaped, so an
            # employee's reason text can't inject markup.
            tmpl = _("<b>Adjustment requested</b> by %s<br/>"
                     "Current: %s &#8594; %s<br/>"
                     "Proposed: %s &#8594; %s<br/>"
                     "Change: <b>%s</b>")
            body = Markup(tmpl) % (
                r.requested_by.name or '',
                fmt(r.current_check_in), fmt(r.current_check_out),
                fmt(r.proposed_check_in), fmt(r.proposed_check_out),
                r._fmt_delta(r.hours_delta))
            if r.reason:
                body += Markup("<br/>%s: %s") % (_("Reason"), r.reason)
            r._post_message(body)

    # === HUMAN ===
    # The supervisor accepts the request: the real punch is updated to the
    # proposed times and the request is marked Applied.
    # === AI AGENT ===
    # Guard: only acts on 'suggested'. Writing the attendance (sudo) triggers
    # the hr.attendance hook -> reopens the timecard to draft for re-approval.
    # Callers must ensure the user is the approver/officer (controller/view do).
    def action_apply(self):
        """Approver applies the suggestion to the real attendance record."""
        def _fmt(r, dt):
            if not dt:
                return '—'
            return fields.Datetime.context_timestamp(r, dt).strftime(
                '%m/%d/%Y %I:%M %p')

        for r in self:
            if r.state != 'suggested':
                continue
            if r.attendance_id and (r.proposed_check_in or r.proposed_check_out):
                vals = {}
                if r.proposed_check_in:
                    vals['check_in'] = r.proposed_check_in
                if r.proposed_check_out:
                    vals['check_out'] = r.proposed_check_out
                # Writing the punch reopens the period for re-approval.
                r.attendance_id.sudo().write(vals)
            r.state = 'applied'
            # Rich chatter on the attendance line: who, the times now set, delta.
            tmpl = _("<b>Adjustment applied</b> by %s.<br/>"
                     "Check in: %s<br/>Check out: %s<br/>"
                     "Net change: <b>%s</b>")
            r._post_message(Markup(tmpl) % (
                self.env.user.name,
                _fmt(r, r.proposed_check_in), _fmt(r, r.proposed_check_out),
                r._fmt_delta(r.hours_delta)))
        return True

    # === HUMAN ===
    # The supervisor declines the request; nothing about the punch changes.
    # === AI AGENT ===
    # Only acts on 'suggested'; logs the rejection to chatter. Terminal state.
    def action_reject(self):
        for r in self:
            if r.state != 'suggested':
                continue
            r.state = 'rejected'
            r._post_message(
                Markup(_("Adjustment request rejected by %s.")) % self.env.user.name)
        return True
