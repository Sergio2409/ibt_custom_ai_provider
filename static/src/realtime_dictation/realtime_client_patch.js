import RealtimeClient from "@ai/core/realtime_client";
import { patch } from "@web/core/utils/patch";

const IBT_TOKEN_PREFIX = "ibt.";

/**
 * Session tokens issued through the IBT speech gateway (see
 * ibt_custom_ai_provider/services/speech.py) are "ibt." + base64url JSON
 * {url, secret}: the gateway's WebSocket URL and a single-use secret.
 *
 * @param {string} sessionToken
 * @returns {{url: string, secret: string}|null} null for any other token
 */
export function parseIbtSessionToken(sessionToken) {
    if (typeof sessionToken !== "string" || !sessionToken.startsWith(IBT_TOKEN_PREFIX)) {
        return null;
    }
    try {
        const encoded = sessionToken.slice(IBT_TOKEN_PREFIX.length).replace(/-/g, "+").replace(/_/g, "/");
        const { url, secret } = JSON.parse(atob(encoded));
        if (typeof url !== "string" || !/^wss?:\/\//.test(url) || typeof secret !== "string" || !secret) {
            return null;
        }
        return { url, secret };
    } catch {
        return null;
    }
}

patch(RealtimeClient.prototype, {
    connect(sessionToken) {
        const session = parseIbtSessionToken(sessionToken);
        if (!session) {
            return super.connect(...arguments);
        }
        // Same OpenAI realtime protocol, but on the company's speech gateway.
        this.socket = new WebSocket(session.url, [
            "realtime",
            `openai-insecure-api-key.${session.secret}`,
        ]);
        this.socket.addEventListener("message", this.messageListener);
    },
});
