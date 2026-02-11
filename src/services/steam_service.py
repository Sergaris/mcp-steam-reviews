import httpx
import urllib.parse
import re
from typing import Optional
from src.config.settings import CONFIG
from src.models.review import SteamReview

class SteamService:
    """Сервис для взаимодействия с Steam API."""
    
    def __init__(self) -> None:
        self._headers = {"User-Agent": CONFIG.USER_AGENT}
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=CONFIG.REQUEST_TIMEOUT,
            follow_redirects=True
        )

    async def __aenter__(self) -> "SteamService":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._client.aclose()

    async def get_app_id(self, game_input: str) -> tuple[Optional[str], Optional[str]]:
        """
        Ищет AppID и название игры.
        game_input может быть названием игры или прямой ссылкой на Steam Store.
        Возвращает (appid, game_name).
        """
        # 1. Проверяем, не является ли ввод ссылкой
        app_id_match = re.search(r"(?:https?://)?(?:www\.)?store\.steampowered\.com/app/(\d+)", game_input)
        if app_id_match:
            appid = app_id_match.group(1)
            # Пытаемся получить название через API, так как в ссылке оно может быть неточным или отсутствовать
            name = await self.get_app_name(appid)
            return appid, name or f"AppID {appid}"

        # 2. Поиск по названию
        encoded_term = urllib.parse.quote(game_input)
        url = f"https://store.steampowered.com/api/storesearch/?term={encoded_term}&l=english&cc=US"
        
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
            
            if data.get("total", 0) > 0 and data.get("items"):
                item = data["items"][0]
                return str(item["id"]), item["name"]
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        return None, None

    async def get_app_name(self, appid: str) -> Optional[str]:
        """Получает официальное название игры по AppID."""
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english"
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
            
            app_data = data.get(appid)
            if app_data and app_data.get("success"):
                return app_data.get("data", {}).get("name")
        except (httpx.HTTPError, ValueError, KeyError):
            # В случае ошибки возвращаем None, чтобы вызывающая сторона могла использовать fallback
            pass
        return None

    async def fetch_reviews(
        self, 
        appid: str, 
        review_type: str, 
        target_count: int, 
        sort_by: str = CONFIG.SORT_BY_ALL
    ) -> list[SteamReview]:
        """
        Загружает отзывы, фильтрует их и применяет стратифицированную выборку.
        """
        buffer: list[SteamReview] = []
        cursor = "*"
        attempts = 0
        
        api_review_type = CONFIG.REVIEW_TYPE_POSITIVE if "pos" in review_type.lower() else CONFIG.REVIEW_TYPE_NEGATIVE

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
            
            if sort_by == CONFIG.SORT_BY_ALL:
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
                    playtime_forever = r.get("author", {}).get("playtime_forever", 0)
                    hours = playtime_forever / CONFIG.MINUTES_IN_HOUR
                    text = r.get("review", "").strip()
                    
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
                
            except (httpx.HTTPError, ValueError, KeyError):
                break
        
        # Сортируем буфер по полезности (базовая сортировка Steam)
        buffer.sort(key=lambda x: x.votes_up, reverse=True)
                
        # 2. Стратифицированная выборка
        return self._get_stratified_sample(buffer, target_count)

    def _get_stratified_sample(self, reviews: list[SteamReview], total_target: int) -> list[SteamReview]:
        """Распределяет отзывы по стратам согласно процентам в CONFIG."""
        strata_buckets: dict[str, list[SteamReview]] = {name: [] for name in CONFIG.STRATA}
        
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

    def sort_and_arrange_reviews(self, pos_reviews: list[SteamReview], neg_reviews: list[SteamReview]) -> list[SteamReview]:
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
        # Ветеранские негативы согласно порогу
        vet_neg_candidates = [r for r in neg_sorted if r.hours_played >= CONFIG.VETERAN_PLAYTIME_THRESHOLD]
        # Самый helpful негатив вообще
        most_helpful_neg = sorted(neg_sorted, key=lambda r: r.votes_up, reverse=True)[0] if neg_sorted else None
        
        # Формируем хвост: ветераны-негативы + самый хелпфул негатив
        tail_negatives = []
        if vet_neg_candidates:
            # Берем топовых ветеранов-негативов согласно лимиту, исключая самый хелпфул если он там есть
            count = min(CONFIG.TAIL_VETERANS_COUNT, len(vet_neg_candidates))
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
