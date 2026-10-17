import { Message } from "@mail/core/common/message";
import { onMounted } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";

patch(Message.prototype, {
    setup() {
        super.setup();
        // Voice mode reads the agent's answer aloud when it appears.
        onMounted(() => this.env.services.ibt_voice_reply?.onMessageShown(this.props.message));
    },
});
