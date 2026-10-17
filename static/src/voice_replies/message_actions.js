import { AIMessageActions } from "@ai/discuss/message_actions_patch";
import { registerMessageAction } from "@mail/core/common/message_actions";
import { _t } from "@web/core/l10n/translation";

const ACTION_ID = "ibt-read-aloud";

registerMessageAction(ACTION_ID, {
    condition: ({ channel, message, store }) =>
        Boolean(
            channel?.isAiChat &&
                message.aiIsAgentResponse &&
                store.env.services.ibt_voice_reply.isEnabled(),
        ),
    icon: ({ message, store }) =>
        store.env.services.ibt_voice_reply.isPlaying(message) ? "stop" : "volume_up",
    name: ({ message, store }) =>
        store.env.services.ibt_voice_reply.isPlaying(message) ? _t("Stop reading") : _t("Read aloud"),
    onSelected: ({ message, store }) => {
        const voiceReply = store.env.services.ibt_voice_reply;
        return voiceReply.isPlaying(message) ? voiceReply.stop() : voiceReply.speak(message);
    },
    sequence: 30,
});

// AI chats only show the actions listed there.
AIMessageActions.push(ACTION_ID);
