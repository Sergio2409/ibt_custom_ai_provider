{
    "name": "IBT Custom AI Provider",
    "version": "20.0.1.0.0",
    "category": "Productivity/AI",
    "summary": "Use OpenAI, Anthropic, Ollama, or OpenAI-compatible AI providers",
    "author": "IBT",
    "license": "OPL-1",
    "depends": ["ai", "base_setup"],
    "data": [
        "security/ir.access.csv",
        "data/ai_provider_data.xml",
        "data/ir_cron_data.xml",
        "views/ai_provider_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
}

