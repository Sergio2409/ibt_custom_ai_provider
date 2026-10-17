import { proxy } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { session } from "@web/session";

/** How long voice mode waits for the agent's answer after a voice message. */
export const VOICE_MODE_TIMEOUT_MS = 10 * 60 * 1000;

/**
 * Reads AI agent answers aloud with the company's speech gateway
 * (route /ibt_ai/speech). The audio is fetched part by part, played from
 * memory and released afterwards: nothing is stored.
 */
export const voiceReplyService = {
    dependencies: ["mail.store", "notification"],
    start(env, { "mail.store": store, notification }) {
        const state = proxy({ playingMessageId: null });
        /** channel id -> {afterMessageId, until}: voice mode is waiting for an answer */
        const pendingReplies = new Map();
        let generation = 0;
        let audio = null;

        function isEnabled() {
            return Boolean(session.ibt_voice_replies);
        }

        function isPlaying(message) {
            return state.playingMessageId === message.id;
        }

        function releaseAudio() {
            if (audio) {
                audio.pause();
                URL.revokeObjectURL(audio.src);
                audio = null;
            }
        }

        function stop() {
            generation++;
            releaseAudio();
            state.playingMessageId = null;
        }

        function fetchPart(messageId, part) {
            return rpc("/ibt_ai/speech", { message_id: messageId, part }, { silent: true });
        }

        function playPart(answer, run) {
            return new Promise((resolve, reject) => {
                if (run !== generation) {
                    return resolve();
                }
                const bytes = Uint8Array.from(atob(answer.audio), (char) => char.charCodeAt(0));
                const url = URL.createObjectURL(new Blob([bytes], { type: answer.mimetype }));
                audio = new Audio(url);
                audio.addEventListener("ended", () => {
                    releaseAudio();
                    resolve();
                });
                audio.addEventListener("error", () => reject(new Error("audio playback failed")));
                audio.play().catch(reject);
            });
        }

        async function speak(message) {
            stop();
            const run = generation;
            state.playingMessageId = message.id;
            try {
                let next = fetchPart(message.id, 0);
                for (let part = 0; next; part++) {
                    const answer = await next;
                    if (run !== generation || !answer.parts) {
                        break;
                    }
                    // Fetch the next part while this one plays.
                    next = part + 1 < answer.parts ? fetchPart(message.id, part + 1) : null;
                    await playPart(answer, run);
                }
            } catch {
                if (run === generation) {
                    notification.add(_t("The answer could not be read aloud."), { type: "danger" });
                }
            } finally {
                if (run === generation) {
                    state.playingMessageId = null;
                    releaseAudio();
                }
            }
        }

        /** Voice mode: read the next agent answer in this chat aloud. */
        function expectReply(channelId, afterMessageId) {
            // The company allows voice mode; the user may have switched it off.
            if (session.ibt_voice_mode && store.self_user?.res_users_settings_id?.ibt_voice_mode !== false) {
                pendingReplies.set(channelId, {
                    afterMessageId,
                    until: Date.now() + VOICE_MODE_TIMEOUT_MS,
                });
            }
        }

        /** Called when a message is shown: speak it if voice mode waits for it. */
        function onMessageShown(message) {
            const channelId = message.channel_id?.id;
            const pending = pendingReplies.get(channelId);
            if (!pending) {
                return;
            }
            if (Date.now() > pending.until) {
                pendingReplies.delete(channelId);
                return;
            }
            if (message.aiIsAgentResponse && message.id > pending.afterMessageId) {
                pendingReplies.delete(channelId);
                speak(message);
            }
        }

        return { state, isEnabled, isPlaying, speak, stop, expectReply, onMessageShown };
    },
};

registry.category("services").add("ibt_voice_reply", voiceReplyService);
