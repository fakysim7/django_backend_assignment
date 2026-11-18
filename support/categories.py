SUPPORT_CATEGORIES = (
    {
        "code": "generation",
        "name": "Генерация",
        "description": "Ошибки генерации, зависания, некорректный результат",
    },
    {
        "code": "editor",
        "name": "Документы и редактор",
        "description": "Редактирование, блоки, форматирование, сохранение, изображения",
    },
    {
        "code": "export",
        "name": "Экспорт и скачивание",
        "description": "DOCX, загрузка и проблемы с файлами",
    },
    {
        "code": "account",
        "name": "Аккаунт",
        "description": "Авторизация, профиль, доступ к аккаунту",
    },
    {
        "code": "pricing_tokens",
        "name": "Тарифы и токены",
        "description": "Баланс, лимиты, вопросы по тарифам",
    },
    {
        "code": "technical",
        "name": "Техническая проблема",
        "description": "Ошибки интерфейса, нестабильная работа и прочие технические проблемы",
    },
    {
        "code": "suggestion",
        "name": "Предложение",
        "description": "Идеи, пожелания и улучшения",
    },
    {
        "code": "other",
        "name": "Другое",
        "description": "Обращения, не подходящие под остальные категории",
    },
)

CATEGORY_CHOICES = tuple(
    (category["code"], category["name"]) for category in SUPPORT_CATEGORIES
)
CATEGORY_CODES = frozenset(code for code, _ in CATEGORY_CHOICES)
CATEGORY_UNSPECIFIED = ""
CATEGORY_UNSPECIFIED_FILTER = "unspecified"
