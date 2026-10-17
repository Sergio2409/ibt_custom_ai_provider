import { Composer } from "@mail/core/common/composer";
// Load after the ai module's patch, which adds voice recording to AI chats.
import "@ai/discuss/composer_patch";
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
