import sys
import asyncio
import io
import statistics
from mcp.server.fastmcp import FastMCP  # type: ignore

from src.config.settings import CONFIG
from src.services.steam_service import SteamService

# Инициализация FastMCP
mcp = FastMCP("Steam Reviews")

def get_playtime_distribution_bar(reviews: list, strata: dict) -> str:
    """Генерирует текстовый прогресс-бар распределения playtime."""
    counts = {name: 0 for name in strata}
    for r in reviews:
        for name, bounds in strata.items():
            if bounds["min"] <= r.hours_played < bounds["max"]:
                counts[name] += 1
                break
    
    total = len(reviews)
    if total == 0:
        return "N/A"
        
    bar_parts = []
    for name, count in counts.items():
        pct = count / total
        blocks = int(pct * 10)
        bar_parts.append(f"[{strata[name].get('min', 0)}-{strata[name].get('max', 'inf')}h: {'█' * blocks} {count}]")
    
    return " ".join(bar_parts)

@mcp.tool()
async def get_game_reviews(game_name: str, count: int = 40) -> str:
    """
    Получает структурированные отзывы о игре в Steam для глубокого анализа.
    Использует стратифицированную выборку, чередование тональности и 
    защиту от Recency Bias для оптимальной работы LLM.
    
    Args:
        game_name: Название игры.
        count: Общее количество отзывов (будет разделено поровну между POS/NEG).
    """
    half_count = max(1, count // 2)
    
    async with SteamService() as service:
        # 1. Поиск AppID
        appid = await service.get_app_id(game_name)
        if not appid:
            return f"❌ Ошибка: Игра '{game_name}' не найдена в Steam."
            
        # 2. Параллельное получение отзывов по стратам
        pos_task = service.fetch_reviews(appid, "positive", half_count)
        neg_task = service.fetch_reviews(appid, "negative", half_count)
        
        pos_reviews, neg_reviews = await asyncio.gather(pos_task, neg_task)
        
        if not pos_reviews and not neg_reviews:
            return f"ℹ️ Для игры '{game_name}' (ID: {appid}) отзывы не найдены."
            
        # 3. Расчет статистики
        all_reviews = pos_reviews + neg_reviews
        playtimes = [r.hours_played for r in all_reviews]
        helpfulness = [r.votes_up for r in all_reviews]
        
        median_playtime = statistics.median(playtimes) if playtimes else 0
        median_helpful = statistics.median(helpfulness) if helpfulness else 0
        
        # 4. Подготовка сигналов-якорей
        top_pos = sorted(pos_reviews, key=lambda r: r.votes_up, reverse=True)[0] if pos_reviews else None
        top_neg = sorted(neg_reviews, key=lambda r: r.votes_up, reverse=True)[0] if neg_reviews else None
        
        # 5. Сортировка и чередование (Bias Protection)
        arranged_reviews = service.sort_and_arrange_reviews(pos_reviews, neg_reviews)
        
        # 6. Формирование отчета
        result = [
            f"# Анализ отзывов Steam: {game_name} (AppID: {appid})",
            "",
            "## Метаданные выборки",
            "| Метрика | Значение |",
            "|---------|----------|",
            f"| Всего отзывов | {len(all_reviews)} |",
            f"| Положительных | {len(pos_reviews)} ({len(pos_reviews)/len(all_reviews)*100:.0f}%) |",
            f"| Отрицательных | {len(neg_reviews)} ({len(neg_reviews)/len(all_reviews)*100:.0f}%) |",
            f"| Медианный playtime | {median_playtime:.1f}h |",
            f"| Медианный helpful | {median_helpful:.0f} |",
            "",
            "## Распределение playtime",
            "```",
            f"Playtime: {get_playtime_distribution_bar(all_reviews, CONFIG.STRATA)}",
            "```",
            "",
            "## Топ-сигналы (для калибровки)",
            ""
        ]
        
        if top_pos:
            result.append(f"**Самый helpful положительный ({top_pos.hours_played:.1f}h, helpful: {top_pos.votes_up}):**")
            result.append(f"> {top_pos.text[:200]}...")
            result.append("")
            
        if top_neg:
            result.append(f"**Самый helpful отрицательный ({top_neg.hours_played:.1f}h, helpful: {top_neg.votes_up}):**")
            result.append(f"> {top_neg.text[:200]}...")
            result.append("")
            
        result.append("---")
        result.append("## Все отзывы")
        result.append("")
        
        # Добавляем отзывы в расширенном формате
        for r in arranged_reviews:
            result.append(r.format_for_ai())
            result.append("")
            
        result.append("---")
        result.append("## Инструкция для анализа")
        result.append("Отзывы предоставлены в формате:")
        result.append("- Чередование положительных и отрицательных (для нейтрализации tonal bias)")
        result.append("- Сортировка по весу (playtime × helpful)")
        result.append("- Метаданные: `[Sentiment | Playtime | Helpful | Date]`")
        result.append("- Конец списка содержит критически важные негативы от ветеранов (Bias Protection)")
        result.append("")
        result.append("**Задание для AI:**")
        result.append("1. Проанализируй отзывы, обращая особое внимание на те, где helpful > 100.")
        result.append("2. Изучи негативные отзывы с playtime > 200h — они содержат наиболее глубокую критику.")
        result.append("3. Выдели 3-5 ключевых паттернов (проблем или достоинств), повторяющихся в разных группах игроков.")
        result.append("4. Оцени, как меняется восприятие игры по мере увеличения времени в ней.")
        
        return "\n".join(result)

if __name__ == "__main__":
    # Убеждаемся, что вывод в UTF-8
    if sys.stdout.encoding.lower() != 'utf-8':
        try:
            if isinstance(sys.stdout, io.TextIOWrapper):
                sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
            
    mcp.run()
