{
    "name": "IBT Custom AI Provider",
    "version": "20.0.1.6.0",
    "category": "Productivity/AI",
    "summary": "Use OpenAI, Anthropic, Ollama, or OpenAI-compatible AI providers, and your own speech gateway",
    "author": "IBT",
    "license": "OPL-1",
    "depends": ["ai", "base_setup"],
    "data": [
        "security/ir.access.csv",
        "data/ai_provider_data.xml",
        "data/ir_cron_data.xml",
        "views/ai_request_log_views.xml",
        "views/ai_provider_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ibt_custom_ai_provider/static/src/**/*",
        ],
        "web.assets_unit_tests": [
            "ibt_custom_ai_provider/static/tests/**/*",
        ],
    },
    "installable": True,
    "application": False,
}

