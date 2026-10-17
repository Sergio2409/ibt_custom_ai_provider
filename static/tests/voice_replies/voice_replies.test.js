import { createAIChat, defineAIModels } from "@ai/../tests/ai_test_helpers";
import { click, contains, openDiscuss, start, startServer } from "@mail/../tests/mail_test_helpers";
import { describe, expect, test } from "@odoo/hoot";
import { getService, onRpc, patchWithCleanup, serverState } from "@web/../tests/web_test_helpers";
import { session } from "@web/session";

describe.current.tags("desktop");
defineAIModels();

class FakeAudio {
    constructor(src) {
        this.src = src;
        this.listeners = {};
    }
    addEventListener(type, listener) {
        this.listeners[type] = listener;
    }
    play() {
        expect.step("play");
        setTimeout(() => this.listeners.ended?.());
        return Promise.resolve();
    }
    pause() {}
}

async function setupAiChat() {
    const pyEnv = await startServer();
    const agentPartnerId = pyEnv["res.partner"].create({ name: "Agent Partner" });
    const agentId = pyEnv["ai.agent"].create({ name: "Agent Partner", partner_id: agentPartnerId });
    const { channelId } = createAIChat(pyEnv, agentId);
    const [commentSubtypeId] = pyEnv["mail.message.subtype"].search([
        ["subtype_xmlid", "=", "mail.mt_comment"],
    ]);
    pyEnv["mail.message"].create([
        {
            author_id: serverState.partnerId,
            body: "¿Cuántas facturas vencidas tengo?",
            message_type: "comment",
            model: "discuss.channel",
            res_id: channelId,
            subtype_id: commentSubtypeId,
        },
        {
            author_id: agentPartnerId,
            body: "<p>Tienes tres facturas vencidas.</p>",
            message_type: "comment",
            model: "discuss.channel",
            res_id: channelId,
            subtype_id: commentSubtypeId,
        },
    ]);
    onRpc("/ibt_ai/speech", async (request) => {
        const { params } = await request.json();
        expect.step(`speech part ${params.part}`);
        return { parts: 2, part: params.part, mimetype: "audio/mpeg", audio: btoa("fake mp3") };
    });
    patchWithCleanup(window, { Audio: FakeAudio });
    return { channelId };
}

test("the speaker button reads an agent answer aloud, part by part", async () => {
    patchWithCleanup(session, { ibt_voice_replies: true });
    const { channelId } = await setupAiChat();
    await start();
    await openDiscuss(channelId);
    await click(".o-mail-Message.o-ai-agent-response button[name='ibt-read-aloud']");
    // Part 1 is requested before part 0 plays (prefetch); the mock server
    // records the request once it has read its body, after the first play.
    await expect.waitForSteps(["speech part 0", "play", "speech part 1", "play"]);
});

test("no speaker button when voice replies are off", async () => {
    patchWithCleanup(session, { ibt_voice_replies: false });
    const { channelId } = await setupAiChat();
    await start();
    await openDiscuss(channelId);
    await contains(".o-mail-Message.o-ai-agent-response");
    await contains("button[name='ibt-read-aloud']", { count: 0 });
});

test("voice mode reads only a newer agent answer in the same chat", async () => {
    patchWithCleanup(session, { ibt_voice_replies: true, ibt_voice_mode: true });
    const { channelId } = await setupAiChat();
    await start();
    const voiceReply = getService("ibt_voice_reply");

    voiceReply.expectReply(channelId, 100);
    voiceReply.onMessageShown({ id: 99, channel_id: { id: channelId }, aiIsAgentResponse: true });
    voiceReply.onMessageShown({ id: 101, channel_id: { id: channelId + 1 }, aiIsAgentResponse: true });
    voiceReply.onMessageShown({ id: 102, channel_id: { id: channelId }, aiIsAgentResponse: false });
    expect.verifySteps([]);

    voiceReply.onMessageShown({ id: 103, channel_id: { id: channelId }, aiIsAgentResponse: true });
    await expect.waitForSteps(["speech part 0", "play", "speech part 1", "play"]);

    // Only once per voice message.
    voiceReply.onMessageShown({ id: 104, channel_id: { id: channelId }, aiIsAgentResponse: true });
    expect.verifySteps([]);
});

test("voice mode not allowed by the company: no switch, no automatic reading", async () => {
    patchWithCleanup(session, { ibt_voice_replies: true, ibt_voice_mode: false });
    const { channelId } = await setupAiChat();
    await start();
    await openDiscuss(channelId);
    await click("button[title='More Actions']");
    await contains("button[name='toggle-web-search']");
    await contains("button[name='ibt-voice-mode']", { count: 0 });
    const voiceReply = getService("ibt_voice_reply");
    voiceReply.expectReply(channelId, 0);
    voiceReply.onMessageShown({ id: 5, channel_id: { id: channelId }, aiIsAgentResponse: true });
    expect.verifySteps([]);
});

test("each user can switch voice mode off in the AI chat", async () => {
    patchWithCleanup(session, { ibt_voice_replies: true, ibt_voice_mode: true });
    const { channelId } = await setupAiChat();
    onRpc("res.users.settings", "set_res_users_settings", ({ kwargs }) => {
        expect.step(`saved voice mode ${kwargs.new_settings.ibt_voice_mode}`);
    });
    await start();
    await openDiscuss(channelId);
    await click("button[title='More Actions']");
    await click("button[name='ibt-voice-mode']");
    await expect.waitForSteps(["saved voice mode false"]);
    await contains("button[name='ibt-voice-mode'] input[type='checkbox']:not(:checked)");

    const voiceReply = getService("ibt_voice_reply");
    voiceReply.expectReply(channelId, 0);
    voiceReply.onMessageShown({ id: 5, channel_id: { id: channelId }, aiIsAgentResponse: true });
    expect.verifySteps([]);
});
