import { describe, expect, test } from "@odoo/hoot";
import { patchWithCleanup } from "@web/../tests/web_test_helpers";
import RealtimeClient from "@ai/core/realtime_client";
import { parseIbtSessionToken } from "@ibt_custom_ai_provider/realtime_dictation/realtime_client_patch";

function ibtToken(payload) {
    return "ibt." + btoa(JSON.stringify(payload)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

class FakeWebSocket {
    static instances = [];
    constructor(url, protocols) {
        this.url = url;
        this.protocols = protocols;
        this.listeners = [];
        FakeWebSocket.instances.push(this);
    }
    addEventListener(type, listener) {
        this.listeners.push(type);
    }
}

describe.current.tags("desktop");

test("parses gateway session tokens and ignores others", () => {
    const token = ibtToken({ url: "wss://speech.example.com/v1/realtime", secret: "ek_abc" });
    expect(parseIbtSessionToken(token)).toEqual({ url: "wss://speech.example.com/v1/realtime", secret: "ek_abc" });
    expect(parseIbtSessionToken("eyJhbGciOi.openai-token")).toBe(null);
    expect(parseIbtSessionToken("ibt.not-base64-json")).toBe(null);
    expect(parseIbtSessionToken(ibtToken({ url: "javascript:alert(1)", secret: "x" }))).toBe(null);
    expect(parseIbtSessionToken(ibtToken({ url: "wss://speech.example.com/v1/realtime" }))).toBe(null);
    expect(parseIbtSessionToken(undefined)).toBe(null);
});

test("connects to the gateway with the secret as subprotocol", () => {
    FakeWebSocket.instances = [];
    patchWithCleanup(window, { WebSocket: FakeWebSocket });
    const client = new RealtimeClient();
    client.connect(ibtToken({ url: "wss://speech.example.com/v1/realtime", secret: "ek_abc" }));
    const [socket] = FakeWebSocket.instances;
    expect(socket.url).toBe("wss://speech.example.com/v1/realtime");
    expect(socket.protocols).toEqual(["realtime", "openai-insecure-api-key.ek_abc"]);
    expect(socket.listeners).toEqual(["message"]);
});

test("other tokens keep Odoo's OpenAI connection", () => {
    FakeWebSocket.instances = [];
    patchWithCleanup(window, { WebSocket: FakeWebSocket });
    new RealtimeClient().connect("openai-session-token");
    const [socket] = FakeWebSocket.instances;
    expect(socket.url).toBe("wss://api.openai.com/v1/realtime");
    expect(socket.protocols).toEqual(["realtime", "openai-insecure-api-key.openai-session-token"]);
});
