from dataclasses import dataclass, field
from typing import Dict

@dataclass(frozen=True)
class SteamConfig:
    """Централизованные настройки для работы с Steam API."""
    
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    REQUEST_TIMEOUT: int = 15
    
    # Количество отзывов для AI по умолчанию
    DEFAULT_REVIEW_COUNT: int = 25 # 25 POS + 25 NEG = 50 total
    
    # Константы для поиска
    # Используем 100 лет (36500 дней) для получения "All Time" отзывов
    ALL_TIME_DAYS: int = 36500
    
    # Параметры фильтрации
    MIN_PLAYTIME: float = 2.0
    MIN_TEXT_LENGTH: int = 50
    
    # Буфер для выборки (сколько отзывов тянуть из API перед стратификацией)
    FETCH_BUFFER_SIZE: int = 300
    
    # Страты выборки
    STRATA: Dict[str, Dict] = field(default_factory=lambda: {
        "Beginner": {"min": 2.0, "max": 20.0, "pct": 0.20},
        "Intermediate": {"min": 20.0, "max": 100.0, "pct": 0.40},
        "Veteran": {"min": 100.0, "max": 500.0, "pct": 0.30},
        "Hardcore": {"min": 500.0, "max": float('inf'), "pct": 0.10}
    })
    
    # Лимиты API
    MAX_PER_PAGE: int = 100
    MAX_API_ATTEMPTS: int = 10  # Увеличим для заполнения страт

    # Константы для форматирования и логики
    MINUTES_IN_HOUR: float = 60.0
    VETERAN_PLAYTIME_THRESHOLD: float = 500.0
    TAIL_VETERANS_COUNT: int = 5
    PREVIEW_TEXT_LENGTH: int = 200
    HELPFUL_THRESHOLD: int = 100
    DEEP_CRITIC_PLAYTIME: int = 200
    BAR_BLOCKS_COUNT: int = 10
    
    # Ревью и лимиты
    MIN_REVIEWS_PER_SENTIMENT: int = 1
    SENTIMENT_DIVISOR: int = 2
    DATE_FORMAT: str = "%Y-%m-%d"
    
    # Константы для типов отзывов
    REVIEW_TYPE_POSITIVE: str = "positive"
    REVIEW_TYPE_NEGATIVE: str = "negative"
    SORT_BY_ALL: str = "all"

CONFIG = SteamConfig()
