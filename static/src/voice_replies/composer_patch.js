import { Composer } from "@mail/core/common/composer";
import { ComposerAction, registerComposerAction } from "@mail/core/common/composer_actions";
// Load after the ai module's patches, which add voice recording and the AI toggles.
import "@ai/discuss/composer_patch";
import "@ai/discuss/composer_actions_patch";
import { AiSessionToggle } from "@ai/components/ai_composer_actions_selectors/ai_session_toggle";
import { _t } from "@web/core/l10n/translation";
import { session } from "@web/session";
import { onWillDestroy, useOnChange } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";

patch(Composer.prototype, {
    setup() {
        super.setup();
        // Voice mode: remember that the composer text came from a voice message.
        this.ibtVoiceDraft = false;
        this.ibtWasTranscribing = false;
        const voiceReply = this.env.services.ibt_voice_reply;
        if (this.props.composer.targetThread?.channel?.isAiChat && this.voiceRecorder) {
            // The avatar shows the recording (listening) and its loudness.
            voiceReply.state.recorder = this.voiceRecorder;
            onWillDestroy(() => {
                if (voiceReply.state.recorder === this.voiceRecorder) {
                    voiceReply.state.recorder = null;
                }
            });
        }
        if (this.voiceTranscription) {
            // Reactive: runs when the transcription starts or ends, not on render.
            useOnChange(
                () => [this.voiceTranscription.isTranscribing],
                (isTranscribing) => {
                    voiceReply.state.transcribing = isTranscribing;
                    if (isTranscribing) {
                        this.ibtWasTranscribing = true;
                    } else if (this.ibtWasTranscribing) {
                        this.ibtWasTranscribing = false;
                        this.ibtVoiceDraft = true;
                    }
                },
                { initialRun: false },
            );
        }
    },
    async sendMessage() {
        const channel = this.props.composer.targetThread?.channel;
        // Sent while recording (the ai patch transcribes first) or with transcribed text.
        const fromVoice = Boolean(
            channel?.isAiChat &&
                (this.voiceRecorder?.recording ||
                    this.voiceTranscription?.isTranscribing ||
                    this.ibtVoiceDraft),
        );
        this.ibtVoiceDraft = false;
        const afterMessageId = this.props.composer.targetThread?.newestPersistentMessage?.id ?? 0;
        const result = await super.sendMessage(...arguments);
        if (fromVoice || this.env.services.ibt_voice_reply.isAvatarOn()) {
            this.env.services.ibt_voice_reply.expectReply(channel.id, afterMessageId);
        }
        return result;
    },
});

patch(ComposerAction.prototype, {
    _getAiComposerActions() {
        return [...super._getAiComposerActions(), "ibt-voice-mode", "ibt-voice-avatar"];
    },
});

/** Per-user switch in the AI chat menu, shown when the company allows voice mode. */
function userToggle(field, name, icon, sequence) {
    return {
        condition: ({ composer }) => composer.targetThread?.channel?.isAiChat && session.ibt_voice_mode,
        closingModeAsDropdown: "none",
        extraContentComponent: AiSessionToggle,
        extraContentComponentProps: ({ store }) => ({
            aiSession: { id: field, config: { [field]: store.env.services.ibt_voice_reply.userSetting(field) } },
            configAttribute: field,
        }),
        icon,
        name,
        onSelected({ store }) {
            const service = store.env.services.ibt_voice_reply;
            service.setUserSetting(field, !service.userSetting(field));
        },
        sequence,
        sequenceGroup: 20,
    };
}

registerComposerAction("ibt-voice-mode", userToggle("ibt_voice_mode", _t("Voice Mode"), "volume_up", 25));
registerComposerAction("ibt-voice-avatar", userToggle("ibt_voice_avatar", _t("Voice Avatar"), "graphic_eq", 26));
