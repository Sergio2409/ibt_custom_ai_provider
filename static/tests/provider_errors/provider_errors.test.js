import { expect, test } from "@odoo/hoot";
import { WarningDialog } from "@web/core/errors/error_dialogs";
import { registry } from "@web/core/registry";

test("provider errors are shown as warnings, not as server errors", () => {
    const dialog = registry
        .category("error_dialogs")
        .get("odoo.addons.ibt_custom_ai_provider.services.client.ProviderError");
    expect(dialog).toBe(WarningDialog);
});
