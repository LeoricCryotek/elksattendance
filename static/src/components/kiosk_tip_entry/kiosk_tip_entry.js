/** @odoo-module **/
import { Component, useState, onWillDestroy } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";

/**
 * Kiosk Tip Entry component.
 *
 * Shown after a tipped employee clocks out.  Presents a numeric key-pad
 * (with decimal point) so the employee can enter their gratuity for the
 * shift.  Supports "Skip" (no tip recorded) and "OK" (save amount).
 *
 * Modeled closely after KioskPinCode so the look-and-feel is consistent
 * with the rest of the kiosk experience.
 */
export class KioskTipEntry extends Component {
    static template = "elksattendance.KioskTipEntry";
    static props = {
        employeeData: { type: Object },
        onTipConfirm: { type: Function },
        onTipSkip: { type: Function },
    };

    setup() {
        this.padButtons = [
            ...Array.from({ length: 9 }, (_, i) => [i + 1]),  // 1-9
            [".", "btn-secondary"],
            [0],
            ["C", "btn-warning"],
        ];
        this.state = useState({
            tipValue: "",
        });
        this.lockPad = false;

        const onKeyDown = async (ev) => {
            const key = ev.key;

            if (key >= "0" && key <= "9") {
                ev.preventDefault();
                this.onClickPadButton(parseInt(key));
            } else if (key === ".") {
                ev.preventDefault();
                this.onClickPadButton(".");
            } else if (key === "Delete") {
                ev.preventDefault();
                this.onClickPadButton("C");
            } else if (key === "Backspace") {
                ev.preventDefault();
                this.state.tipValue = this.state.tipValue.substring(
                    0, this.state.tipValue.length - 1
                );
            } else if (key === "Enter") {
                ev.preventDefault();
                await this.onConfirm();
            } else if (key === "Escape") {
                ev.preventDefault();
                this.props.onTipSkip();
            }
        };

        browser.addEventListener("keydown", onKeyDown);
        onWillDestroy(() => browser.removeEventListener("keydown", onKeyDown));
    }

    get displayValue() {
        const val = this.state.tipValue;
        if (!val) return "$0.00";
        const num = parseFloat(val);
        if (isNaN(num)) return "$0.00";
        // Show whatever they're typing with a dollar sign
        return "$" + val;
    }

    onClickPadButton(value) {
        if (this.lockPad) return;

        if (value === "C") {
            this.state.tipValue = "";
            return;
        }

        if (value === ".") {
            // Only allow one decimal point
            if (this.state.tipValue.includes(".")) return;
            if (!this.state.tipValue) {
                this.state.tipValue = "0.";
                return;
            }
        }

        // Limit to 2 decimal places
        const parts = this.state.tipValue.split(".");
        if (parts.length === 2 && parts[1].length >= 2) return;

        // Limit total length
        if (this.state.tipValue.length >= 8) return;

        this.state.tipValue += String(value);
    }

    async onConfirm() {
        if (this.lockPad) return;
        this.lockPad = true;
        const amount = parseFloat(this.state.tipValue) || 0;
        await this.props.onTipConfirm(amount);
        this.state.tipValue = "";
        this.lockPad = false;
    }
}
