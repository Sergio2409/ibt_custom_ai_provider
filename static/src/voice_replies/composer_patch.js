import { Composer } from "@mail/core/common/composer";
import { ComposerAction, registerComposerAction } from "@mail/core/common/composer_actions";
// Load after the ai module's patches, which add voice recording and the AI toggles.
import "@ai/discuss/composer_patch";
import "@ai/discuss/composer_actions_patch";
import { AiSessionToggle } from "@ai/components/ai_composer_actions_selectors/ai_session_toggle";
import { _t } from "@web/core/l10n/translation";
import { session } from "@web/session";
import { useOnChange } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";

patch(Composer.prototype, {
    setup() {
        super.setup();
        // Voice mode: remember that the composer text came from a voice message.
        this.ibtVoiceDraft = false;
        this.ibtWasTranscribing = false;
        if (this.voiceTranscription) {
            // Reactive: runs when the transcription starts or ends, not on render.
            useOnChange(
                () => [this.voiceTranscription.isTranscribing],
                (isTranscribing) => {
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
        if (fromVoice) {
            this.env.services.ibt_voice_reply.expectReply(channel.id, afterMessageId);
        }
        return result;
    },
});

patch(ComposerAction.prototype, {
    _getAiComposerActions() {
        return [...super._getAiComposerActions(), "ibt-voice-mode"];
    },
});

/** Each user switches voice mode for themselves, when the company allows it. */
registerComposerAction("ibt-voice-mode", {
    condition: ({ composer }) => composer.targetThread?.channel?.isAiChat && session.ibt_voice_mode,
    closingModeAsDropdown: "none",
    extraContentComponent: AiSessionToggle,
    extraContentComponentProps: ({ store }) => ({
        aiSession: { id: "ibt-voice-mode", config: store.self_user.res_users_settings_id },
        configAttribute: "ibt_voice_mode",
    }),
    icon: "record_voice_over",
    name: _t("Voice Mode"),
    onSelected({ store }) {
        const settings = store.self_user.res_users_settings_id;
        settings.ibt_voice_mode = settings.ibt_voice_mode === false; // unset counts as on
        store.env.services.orm.call("res.users.settings", "set_res_users_settings", [[settings.id]], {
            new_settings: { ibt_voice_mode: settings.ibt_voice_mode },
        });
    },
    sequence: 25,
    sequenceGroup: 20,
});
