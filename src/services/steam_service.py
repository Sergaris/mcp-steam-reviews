import httpx
import urllib.parse
import asyncio
import re
import math
from typing import Optional, Dict, List, Tuple
from src.config.settings import CONFIG
from src.models.review import SteamReview

class SteamService:
    """Сервис для взаимодействия с Steam API."""
    
    def __init__(self):
        self._headers = {"User-Agent": CONFIG.USER_AGENT}
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=CONFIG.REQUEST_TIMEOUT,
            follow_redirects=True
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._client.aclose()

    async def get_app_id(self, game_name: str) -> Optional[str]:
        """Ищет AppID игры по названию."""
        encoded_term = urllib.parse.quote(game_name)
        url = f"https://store.steampowered.com/api/storesearch/?term={encoded_term}&l=english&cc=US"
        
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
            
            if data.get("total", 0) > 0 and data.get("items"):
                # Берем самый релевантный первый результат
                return str(data["items"][0]["id"])
        except Exception:
            pass
        return None

    async def fetch_reviews(
        self, 
        appid: str, 
        review_type: str, 
        target_count: int, 
        sort_by: str = "all"
    ) -> list[SteamReview]:
        """
        Загружает отзывы, фильтрует их и применяет стратифицированную выборку.
        """
        buffer: list[SteamReview] = []
        cursor = "*"
        attempts = 0
        
        # Определяем тип для API
        api_review_type = "positive" if "pos" in review_type.lower() else "negative"
        
        # 1. Набор буфера отзывов
        while len(buffer) < CONFIG.FETCH_BUFFER_SIZE and attempts < CONFIG.MAX_API_ATTEMPTS:
            attempts += 1
            
            params = {
                "json": 1,
                "filter": sort_by,
                "language": "all",
                "review_type": api_review_type,
                "num_per_page": CONFIG.MAX_PER_PAGE,
                "cursor": cursor,
            }
            
            if sort_by == "all":
                params["day_range"] = CONFIG.ALL_TIME_DAYS
                
            url = f"https://store.steampowered.com/appreviews/{appid}"
            
            try:
                response = await self._client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                
                if not data.get("success"):
                    break
                
                new_reviews_data = data.get("reviews", [])
                if not new_reviews_data:
                    break
                    
                for r in new_reviews_data:
                    hours = r.get("author", {}).get("playtime_forever", 0) / 60.0
                    text = r.get("review", "").strip()
                    
                    # ОБЯЗАТЕЛЬНЫЕ ФИЛЬТРЫ
                    if hours < CONFIG.MIN_PLAYTIME or len(text) < CONFIG.MIN_TEXT_LENGTH:
                        continue
                        
                    buffer.append(SteamReview(
                        review_id=str(r.get("recommendationid")),
                        text=text,
                        is_positive=r.get("voted_up", True),
                        hours_played=hours,
                        votes_up=r.get("votes_up", 0),
                        created_at=r.get("timestamp_created", 0),
                        received_for_free=r.get("received_for_free", False)
                    ))
                
                new_cursor = data.get("cursor")
                if not new_cursor or new_cursor == cursor:
                    break
                cursor = new_cursor
                
            except Exception:
                break
        
        # Сортируем буфер по полезности (базовая сортировка Steam)
        buffer.sort(key=lambda x: x.votes_up, reverse=True)
                
        # 2. Стратифицированная выборка
        return self._get_stratified_sample(buffer, target_count)

    def _get_stratified_sample(self, reviews: list[SteamReview], total_target: int) -> list[SteamReview]:
        """Распределяет отзывы по стратам согласно процентам в CONFIG."""
        strata_buckets: Dict[str, List[SteamReview]] = {name: [] for name in CONFIG.STRATA}
        
        # Распределяем по ведрам
        for r in reviews:
            for name, bounds in CONFIG.STRATA.items():
                if bounds["min"] <= r.hours_played < bounds["max"]:
                    strata_buckets[name].append(r)
                    break
        
        result: list[SteamReview] = []
        
        # Выбираем из каждого ведра согласно проценту
        for name, bounds in CONFIG.STRATA.items():
            strat_target = max(1, int(total_target * bounds["pct"]))
            result.extend(strata_buckets[name][:strat_target])
            
        return result

    def sort_and_arrange_reviews(self, pos_reviews: list[SteamReview], neg_reviews: list[SteamReview]) -> List[SteamReview]:
        """
        Реализует оптимальный порядок отображения:
        1. Сортировка по весу (playtime * log(helpful+1)).
        2. Чередование +/-.
        3. Защита от Recency Bias (ветераны-негативы в конце).
        """
        # Сортировка по весу внутри групп
        pos_sorted = sorted(pos_reviews, key=lambda r: r.weight, reverse=True)
        neg_sorted = sorted(neg_reviews, key=lambda r: r.weight, reverse=True)
        
        # Выделяем "якоря" для конца (защита от Recency Bias)
        # 5-10 ветеранских негативов (500h+)
        vet_neg_candidates = [r for r in neg_sorted if r.hours_played >= 500]
        # Самый helpful негатив вообще
        most_helpful_neg = sorted(neg_sorted, key=lambda r: r.votes_up, reverse=True)[0] if neg_sorted else None
        
        # Формируем хвост: 5 ветеранов-негативов (если есть) + самый хелпфул негатив
        tail_negatives = []
        if vet_neg_candidates:
            # Берем до 5 топовых ветеранов-негативов, исключая самый хелпфул если он там есть
            count = min(5, len(vet_neg_candidates))
            tail_negatives = vet_neg_candidates[:count]
            if most_helpful_neg in tail_negatives:
                tail_negatives.remove(most_helpful_neg)
        
        # Основной массив для чередования (исключая то, что пойдет в хвост)
        exclude_ids = {r.review_id for r in tail_negatives}
        if most_helpful_neg:
            exclude_ids.add(most_helpful_neg.review_id)
            
        main_pos = pos_sorted
        main_neg = [r for r in neg_sorted if r.review_id not in exclude_ids]
        
        # Чередование
        final_list = []
        for p, n in zip(main_pos, main_neg):
            final_list.append(p)
            final_list.append(n)
            
        # Добавляем оставшиеся из main (если списки разной длины)
        remaining_p = main_pos[len(main_neg):]
        remaining_n = main_neg[len(main_pos):]
        final_list.extend(remaining_p)
        final_list.extend(remaining_n)
        
        # Добавляем хвост
        final_list.extend(tail_negatives)
        if most_helpful_neg:
            final_list.append(most_helpful_neg)
            
        return final_list
