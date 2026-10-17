import { proxy } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { session } from "@web/session";

/** How long voice mode waits for the agent's answer after a voice message. */
export const VOICE_MODE_TIMEOUT_MS = 10 * 60 * 1000;
/** Per-user switches (res.users.settings) and their value until the server sends one. */
const USER_DEFAULTS = { ibt_voice_mode: true, ibt_voice_avatar: true };

/**
 * Reads AI agent answers aloud with the company's speech gateway
 * (route /ibt_ai/speech). The audio is fetched part by part, played from
 * memory and released afterwards: nothing is stored.
 */
export const voiceReplyService = {
    dependencies: ["mail.store", "notification", "orm"],
    start(env, { "mail.store": store, notification, orm }) {
        // level: loudness of the answer being read (0-1), for the avatar;
        // recorder / transcribing: the AI composer's voice message, for the avatar.
        const state = proxy({ playingMessageId: null, level: 0, recorder: null, transcribing: false });
        /** channel id -> {afterMessageId, until}: voice mode is waiting for an answer */
        const pendingReplies = new Map();
        let generation = 0;
        let audio = null;
        let audioContext = null;

        function userSetting(name) {
            return store.self_user?.res_users_settings_id?.[name] ?? USER_DEFAULTS[name];
        }

        function setUserSetting(name, value) {
            const settings = store.self_user.res_users_settings_id;
            settings[name] = value;
            return orm.call("res.users.settings", "set_res_users_settings", [[settings.id]], {
                new_settings: { [name]: value },
            });
        }

        /** Voice mode is on (company and user) and the user keeps the talking avatar. */
        function isAvatarOn() {
            return Boolean(session.ibt_voice_mode && userSetting("ibt_voice_mode") && userSetting("ibt_voice_avatar"));
        }

        /** Measure the loudness of `element` while it plays (best effort, avatar only). */
        function watchLevel(element) {
            try {
                audioContext ??= new AudioContext();
                audioContext.resume();
                if (audioContext.state !== "running") {
                    return; // no user gesture yet: the avatar uses its CSS animation only
                }
                const analyser = audioContext.createAnalyser();
                audioContext.createMediaElementSource(element).connect(analyser);
                analyser.connect(audioContext.destination);
                const samples = new Uint8Array(analyser.fftSize);
                const tick = () => {
                    if (audio !== element) {
                        state.level = 0;
                        return;
                    }
                    analyser.getByteTimeDomainData(samples);
                    const power = samples.reduce((sum, sample) => sum + ((sample - 128) / 128) ** 2, 0);
                    state.level = Math.min(1, Math.round(Math.sqrt(power / samples.length) * 40) / 10);
                    requestAnimationFrame(tick);
                };
                tick();
            } catch {
                state.level = 0;
            }
        }

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
                if (isAvatarOn()) {
                    watchLevel(audio);
                }
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

        /** Voice mode: read the next agent answer in this chat aloud (always with the avatar). */
        function expectReply(channelId, afterMessageId) {
            // The company allows voice mode; the user may have switched it off.
            if (isAvatarOn() || (session.ibt_voice_mode && userSetting("ibt_voice_mode"))) {
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

        return {
            state,
            isEnabled,
            isPlaying,
            isAvatarOn,
            userSetting,
            setUserSetting,
            speak,
            stop,
            expectReply,
            onMessageShown,
        };
    },
};

registry.category("services").add("ibt_voice_reply", voiceReplyService);
