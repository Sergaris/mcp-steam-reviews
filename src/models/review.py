import math
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class SteamReview:
    """Модель структурированного отзыва для анализа нейросетью."""
    
    review_id: str
    text: str
    is_positive: bool
    hours_played: float
    votes_up: int
    created_at: int # timestamp
    received_for_free: bool
    
    @property
    def weight(self) -> float:
        """Расчет веса отзыва: playtime * log(helpful + 1)."""
        return self.hours_played * math.log(self.votes_up + 1)

    def format_for_ai(self) -> str:
        """Превращает отзыв в расширенный формат с метаданными."""
        sentiment = "POSITIVE" if self.is_positive else "NEGATIVE"
        free_tag = " [FREE PRODUCT]" if self.received_for_free else ""
        date_str = datetime.fromtimestamp(self.created_at).strftime('%Y-%m-%d')
        
        header = f"[{sentiment} | Playtime: {self.hours_played:.1f}h | Helpful: {self.votes_up} | Date: {date_str}]{free_tag}"
        
        # Очистка текста от лишних пробелов
        clean_text = " ".join(self.text.split())
        
        return f"---\n{header}\n{clean_text}\n---"
