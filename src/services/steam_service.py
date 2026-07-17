import httpx
import urllib.parse
import re
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

    async def get_app_id(self, game_input: str) -> tuple[str | None, str | None, bool]:
        """Ищет AppID, название и признак бесплатной игры.

        Args:
            game_input: Название игры или ссылка на страницу Steam Store.

        Returns:
            Кортеж (appid, game_name, is_free). При неудаче — (None, None, False).
        """
        appid: str | None = None
        fallback_name: str | None = None

        # 1. Проверяем, не является ли ввод ссылкой
        app_id_match = re.search(
            r"(?:https?://)?(?:www\.)?store\.steampowered\.com/app/(\d+)",
            game_input,
        )
        if app_id_match:
            appid = app_id_match.group(1)
        else:
            # 2. Поиск по названию
            encoded_term = urllib.parse.quote(game_input)
            url = (
                "https://store.steampowered.com/api/storesearch/"
                f"?term={encoded_term}&l=english&cc=US"
            )
            try:
                response = await self._client.get(url)
                response.raise_for_status()
                data = response.json()
                if data.get("total", 0) > 0 and data.get("items"):
                    item = data["items"][0]
                    appid = str(item["id"])
                    fallback_name = item.get("name")
            except (httpx.HTTPError, ValueError, KeyError):
                return None, None, False

        if not appid:
            return None, None, False

        name, is_free = await self.get_app_metadata(appid)
        return appid, name or fallback_name or f"AppID {appid}", is_free

    async def get_app_metadata(self, appid: str) -> tuple[str | None, bool]:
        """Читает название и флаг is_free из Steam appdetails.

        Args:
            appid: Идентификатор приложения в Steam.

        Returns:
            Кортеж (name, is_free). При ошибке API — (None, False).
        """
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english"
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()

            app_data = data.get(appid)
            if app_data and app_data.get("success"):
                details = app_data.get("data", {})
                name = details.get("name")
                is_free = bool(details.get("is_free", False))
                return name, is_free
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        return None, False

    async def get_app_name(self, appid: str) -> str | None:
        """Получает официальное название игры по AppID."""
        name, _is_free = await self.get_app_metadata(appid)
        return name

    async def fetch_reviews(
        self,
        appid: str,
        review_type: str,
        target_count: int,
        sort_by: str = CONFIG.SORT_BY_ALL,
        *,
        is_free_to_play: bool = False,
    ) -> list[SteamReview]:
        """Загружает отзывы, фильтрует их и применяет стратифицированную выборку.

        Args:
            appid: Идентификатор приложения в Steam.
            review_type: positive / negative (или строка с ``pos`` / иначе negative).
            target_count: Сколько отзывов нужно после стратификации.
            sort_by: Режим сортировки Steam API (по умолчанию all-time).
            is_free_to_play: Если True, запрашивает все лицензии
                (``purchase_type=all``), иначе только покупки Steam.

        Returns:
            Стратифицированная выборка отзывов.
        """
        buffer: list[SteamReview] = []
        cursor = "*"
        attempts = 0

        api_review_type = (
            CONFIG.REVIEW_TYPE_POSITIVE
            if "pos" in review_type.lower()
            else CONFIG.REVIEW_TYPE_NEGATIVE
        )
        purchase_type = (
            CONFIG.PURCHASE_TYPE_ALL
            if is_free_to_play
            else CONFIG.PURCHASE_TYPE_STEAM
        )

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
                "purchase_type": purchase_type,
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
        """Распределяет отзывы по стратам; квоту недобранных страт отдаёт остальным.

        Args:
            reviews: Буфер отзывов, отсортированный по полезности.
            total_target: Сколько отзывов нужно вернуть.

        Returns:
            Выборка размером total_target (или меньше, если данных не хватает физически).
        """
        strata_buckets: dict[str, list[SteamReview]] = {name: [] for name in CONFIG.STRATA}

        for r in reviews:
            for name, bounds in CONFIG.STRATA.items():
                if bounds["min"] <= r.hours_played < bounds["max"]:
                    strata_buckets[name].append(r)
                    break

        result: list[SteamReview] = []
        for name, bounds in CONFIG.STRATA.items():
            strat_target = max(1, int(total_target * bounds["pct"]))
            result.extend(strata_buckets[name][:strat_target])

        # Недобор страт компенсируем лучшими из оставшихся отзывов,
        # чтобы итоговый размер выборки соответствовал запрошенному.
        shortfall = total_target - len(result)
        if shortfall > 0:
            used_ids = {r.review_id for r in result}
            leftovers = [r for r in reviews if r.review_id not in used_ids]
            result.extend(leftovers[:shortfall])

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
