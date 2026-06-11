/** @odoo-module **/
// ============================================================================
// === HUMAN ===
// Inserts the tip-entry screen into the time-clock flow: after a tipped
// employee clocks out, show the keypad, save the tip, then the greeting.
// === AI AGENT ===
// patch() of hr_attendance public_kiosk_app. Adds a 'tips' display state and
// intercepts onManualSelection/onBarcodeScanned to route there when
// employeeData.x_receives_tips and a check_out happened. onTipConfirm calls
// rpc('/hr_attendance/save_tip'). Relies on the controller enriching the
// kiosk response (controllers/kiosk.py).
// ============================================================================
/**
 * Patch the public kiosk app to insert a tip-entry screen between
 * clock-out and the greeting screen for tipped employees.
 *
 * Flow after patch:
 *   clock-out RPC → employee.x_receives_tips?
 *       YES → display "tips" → save tip → display "greet"
 *       NO  → display "greet" (unchanged)
 */
import { patch } from "@web/core/utils/patch";
import { rpc } from "@web/core/network/rpc";
import { KioskTipEntry } from
    "@elksattendance/components/kiosk_tip_entry/kiosk_tip_entry";
import kioskModule from
    "@hr_attendance/public_kiosk/public_kiosk_app";

const { kioskAttendanceApp } = kioskModule;

// Register the tip entry component so the template can reference it
kioskAttendanceApp.components = {
    ...kioskAttendanceApp.components,
    KioskTipEntry,
};

patch(kioskAttendanceApp.prototype, {
    /**
     * Extend switchDisplay to accept the "tips" screen.
     */
    switchDisplay(screen) {
        if (screen === "tips") {
            this.state.active_display = "tips";
            return;
        }
        return super.switchDisplay(screen);
    },

    /**
     * After manual selection clock-out, intercept for tip entry.
     */
    async onManualSelection(employeeId, enteredPin) {
        await super.onManualSelection(employeeId, enteredPin);
        // If we just switched to greet AND the employee receives tips
        // AND this was a clock-out (check_out is truthy), redirect to tips.
        if (
            this.state.active_display === "greet" &&
            this.employeeData &&
            this.employeeData.x_receives_tips &&
            this.employeeData.attendance &&
            this.employeeData.attendance.check_out
        ) {
            this.switchDisplay("tips");
        }
    },

    /**
     * After barcode scan clock-out, intercept for tip entry.
     */
    async onBarcodeScanned(barcode) {
        await super.onBarcodeScanned(barcode);
        if (
            this.state.active_display === "greet" &&
            this.employeeData &&
            this.employeeData.x_receives_tips &&
            this.employeeData.attendance &&
            this.employeeData.attendance.check_out
        ) {
            this.switchDisplay("tips");
        }
    },

    /**
     * Called when the employee confirms their tip amount.
     */
    async onTipConfirm(amount) {
        if (amount > 0 && this.employeeData.attendance.id) {
            await rpc("/hr_attendance/save_tip", {
                token: this.props.token,
                attendance_id: this.employeeData.attendance.id,
                tip_amount: amount,
            });
        }
        this.switchDisplay("greet");
    },

    /**
     * Called when the employee skips tip entry.
     */
    onTipSkip() {
        this.switchDisplay("greet");
    },
});
