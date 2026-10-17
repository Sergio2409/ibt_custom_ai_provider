-- Neutralized copies (staging, tests) must not call providers with production keys.
UPDATE ibt_ai_provider SET api_key_secret = NULL;
