import { odooExceptionTitleMap, WarningDialog } from "@web/core/errors/error_dialogs";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";

// ProviderError is a UserError: show it as a warning, not as a server crash.
const PROVIDER_ERROR = "odoo.addons.ibt_custom_ai_provider.services.client.ProviderError";
registry.category("error_dialogs").add(PROVIDER_ERROR, WarningDialog);
odooExceptionTitleMap.set(PROVIDER_ERROR, _t("AI Provider Error"));
