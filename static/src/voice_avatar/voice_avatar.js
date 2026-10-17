import { Component, useProps, t } from "@odoo/owl";
import { Thread } from "@mail/core/common/thread";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";

const LABELS = {
    listening: _t("Listening..."),
    thinking: _t("Thinking..."),
    speaking: _t("Speaking"),
    idle: _t("Tap the microphone to talk"),
};

/**
 * Voice Avatar: shown over an AI chat instead of its messages. The messages
 * stay mounted underneath, so voice mode still reads each new answer aloud.
 */
export class VoiceAvatar extends Component {
    static template = "ibt_custom_ai_provider.VoiceAvatar";
    props = useProps({ channel: t.object() });

    setup() {
        this.voiceReply = useService("ibt_voice_reply");
        this.labels = LABELS;
    }

    status() {
        const { state } = this.voiceReply;
        if (state.playingMessageId) {
            return "speaking";
        }
        if (state.recorder?.recording) {
            return "listening";
        }
        return state.transcribing || this.props.channel.isAiGenerating ? "thinking" : "idle";
    }

    /** Loudness 0-1: the answer while speaking, the microphone while listening. */
    level(status) {
        const { state } = this.voiceReply;
        if (status === "speaking") {
            return state.level;
        }
        return status === "listening" ? Math.min(1, Math.sqrt(state.recorder.volumes.at(-1) || 0)) : 0;
    }
}

Thread.components = { ...Thread.components, VoiceAvatar };
