# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
"""Контур еды.

Здесь нет ни одного шаблона и ни одного регулярного выражения — намеренно.
Понимание того, что человек говорит о еде, целиком отдано модели: список фраз
вроде «я не завтракал» невозможно перечислить, а любая попытка это сделать
превращается в игру в угадайку, где половина формулировок мимо.

Работает через официальный MCP-сервер ВкусВилла: поиск товаров, состав и КБЖУ,
акции, рецепты, магазины и сборка ссылки на корзину. Если сервер недоступен,
разговор о еде и подсчёт калорий продолжают работать — просто без покупок.
"""

from __future__ import annotations

import json
from html import unescape
import threading
import time
from typing import Any

import httpx

from .trace import log_event

MCP_URL = "https://mcp.vkusvill.ru/mcp"

# Что умеет сервер. Держим отдельно, чтобы не спрашивать его при каждом запуске
# и чтобы модель понимала возможности до первого сетевого вызова.
TOOLS: dict[str, str] = {
    "vkusvill_products_search": "поиск товаров по тексту, с ценой, рейтингом, составом и КБЖУ",
    "vkusvill_product_details": "подробности товара по его id",
    "vkusvill_product_analogs": "аналоги товара",
    "vkusvill_product_barcode": "товар по штрихкоду",
    "vkusvill_products_discount": "товары со скидкой",
    "vkusvill_recipes": "рецепты с ингредиентами, КБЖУ и шагами",
    "vkusvill_shops": "магазины: адреса, часы работы",
    "vkusvill_cart_link_create": "собрать ссылку на корзину",
}



FOOD_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "food": {"type": "boolean"},
        "intent": {"type": "string",
                   "enum": ["chat", "search", "calories", "recipe", "cart", "discount", "shops"]},
        "search_query": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
        "min_rating": {"type": ["number", "null"]},
        "needs_products": {"type": "boolean"},
        "reply_hint": {"type": "string"},
    },
    "required": ["food"],
}

class FoodService:
    """Разговор о еде, калориях и покупках."""

    def __init__(self, settings: Any, gateway: Any, db: Any, memory: Any = None) -> None:
        self.settings = settings
        self.gateway = gateway
        self.db = db
        self.memory = memory
        self._lock = threading.RLock()
        self._session_id: str | None = None
        self._available: bool | None = None
        self._checked_at = 0.0

    # ------------------------------------------------------------ соединение
    def _post(self, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        with httpx.Client(timeout=timeout, trust_env=True) as client:
            # Таймаут задан и на клиенте, и на запросе: при зависшей сети
            # ответ Эрви не должен ждать дольше самой задачи.
            response = client.post(MCP_URL, json=payload, headers=headers, timeout=timeout)
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self._session_id = session
            response.raise_for_status()
            text = response.text.strip()
            # Сервер отвечает либо JSON, либо потоком событий — принимаем оба.
            if text.startswith(("event:", "data:", ":")) or "\ndata:" in text:
                for line in text.splitlines():
                    if line.startswith("data:"):
                        chunk = line[5:].strip()
                        if chunk and chunk != "[DONE]":
                            return json.loads(chunk)
                return {}
            return json.loads(text) if text else {}

    def _handshake(self) -> None:
        response = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "EIRVEN", "version": "2.2.0"},
            },
        })
        if response.get("error") or not response.get("result"):
            raise RuntimeError("Сервер ВкусВилла не подтвердил подключение")
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass

    def available(self, force: bool = False) -> bool:
        """Доступен ли сервер. Результат держим недолго, сеть меняется."""
        now = time.monotonic()
        if not force and self._available is not None and now - self._checked_at < 300:
            return self._available
        with self._lock:
            try:
                self._session_id = None
                self._handshake()
                self._available = True
            except Exception as exc:
                self._available = False
                log_event(self.settings.root_dir, "FOOD_MCP_UNAVAILABLE", error=str(exc)[:200])
            self._checked_at = now
        return bool(self._available)

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 25.0) -> Any:
        """Вызвать инструмент сервера и вернуть разобранный результат."""
        with self._lock:
            if not self._session_id:
                self._handshake()
            data = self._post({
                "jsonrpc": "2.0", "id": int(time.time() * 1000) % 10_000_000,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }, timeout=timeout)
        if "error" in data:
            raise RuntimeError(str(data["error"])[:300])
        result = data.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(str(result.get("content") or "Ошибка MCP ВкусВилла")[:300])
        if result.get("structuredContent") is not None:
            return self._unwrap_result(result["structuredContent"])
        content = result.get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                raw = block.get("text") or ""
                try:
                    parsed = json.loads(raw)
                except (ValueError, TypeError):
                    return raw
                return self._unwrap_result(parsed)
        return result

    @staticmethod
    def _unwrap_result(value: Any) -> Any:
        if isinstance(value, dict):
            if value.get("ok") is False or value.get("error"):
                raise RuntimeError(str(value.get("error") or "Ошибка ВкусВилла")[:300])
            if value.get("ok") is True and "data" in value:
                return value["data"]
        return value

    # ------------------------------------------------------------- понимание
    def understand(self, query: str, context: str = "") -> dict[str, Any]:
        """Решить, относится ли сказанное к еде, и что с этим делать.

        Никаких списков слов: решает модель. Она же формулирует поисковый запрос,
        поэтому «я ещё не завтракал» превращается в поиск завтрака, а не в
        попытку найти товар с таким названием.
        """
        system = (
            "Ты решаешь, относится ли реплика владельца к еде, и что с ней делать.\n"
            "Еда — это не только прямая просьба купить. Сюда же: голод и приёмы пищи, "
            "калории и состав, что приготовить, что заказать, обсуждение продуктов, "
            "диета и ограничения, планирование покупок.\n"
            "Верни ТОЛЬКО JSON без пояснений:\n"
            '{"food": true|false, '
            '"intent": "chat|search|calories|recipe|cart|discount|shops", '
            '"search_query": "КОНКРЕТНЫЙ товар или блюдо для поиска в магазине, по-русски", '
            '"items": ["товар1","товар2"], '
            '"min_rating": число или null, '
            '"needs_products": true|false, '
            '"reply_hint": "о чём ответить человеку одной фразой"}\n'
            "food=false, если о еде речи нет вообще.\n"
            "needs_products=true только если для ответа реально нужны товары из магазина.\n"
            "intent=cart, если человек хочет собрать или заказать набор продуктов.\n"
            "search_query НИКОГДА не повторяет реплику дословно: «я хочу поесть» —\n"
            "это не название товара, магазин по нему ничего не найдёт. Переведи\n"
            "желание в конкретику: «готовые блюда», «горячее», «салаты». Если из "
            "реплики непонятно, что именно искать, ставь needs_products=false и "
            "предложи уточнить — это лучше пустой выдачи.\n\n"
            "Примеры:\n"
            "«я ещё не завтракал» → food=true, intent=search, search_query=завтрак\n"
            "«есть хочу» → food=true, intent=search, search_query=готовые блюда\n"
            "«сколько калорий в сырниках» → food=true, intent=calories, needs_products=true\n"
            "«собери корзину: молоко, хлеб, яйца» → food=true, intent=cart, items=[молоко, хлеб, яйца]\n"
            "«что приготовить из курицы» → food=true, intent=recipe, search_query=курица\n"
            "«что со скидкой» → food=true, intent=discount\n"
            "«где ближайший магазин» → food=true, intent=shops\n"
            "«открой холодильник» → food=false (это про технику, не про еду)\n"
            "«как дела» → food=false"
            "Для просьбы найти поесть, подобрать доставку или проверить наличие ставь "
            "intent=search и needs_products=true. Для заказа одного напитка тоже intent=cart. "
            "items содержит отдельные поисковые названия в именительном падеже: суп, морс, Lipton. "
            "Если человек голоден, предложи товары для нужного приёма пищи. "
            "Короткие продолжения вроде «предложи пару вариантов» понимай по последним репликам. "
            "Если текущая реплика про другую задачу, не переноси в неё тему еды из истории."
        )
        user = f"Реплика владельца: {query}"
        if context:
            user += f"\nКонтекст разговора: {context[-2400:]}"
        try:
            message = self.gateway.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=self.settings.fast_model,
                temperature=0.1, think=False,
                response_format=FOOD_SCHEMA,
                num_ctx=self.settings.chat_num_ctx, num_predict=280,
            )
            raw = str(message.get("content") or "").strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(raw[start:end + 1])
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            log_event(self.settings.root_dir, "FOOD_UNDERSTAND_FAILED", error=str(exc)[:200])
        return {"food": False}

    # -------------------------------------------------------------- действия
    def find_products(self, query: str, *, min_rating: float | None = None,
                      limit: int = 6) -> list[dict[str, Any]]:
        """Товары по запросу, при необходимости отфильтрованные по рейтингу."""
        rows: list[dict[str, Any]] = []
        page = 1
        while len(rows) < limit and page <= 3:
            try:
                data = self.call_tool("vkusvill_products_search", {
                    "q": query, "mode": "full", "sort": "rating", "page": page, "vvonly": 0,
                })
            except Exception as exc:
                log_event(self.settings.root_dir, "FOOD_SEARCH_FAILED", query=query[:80], error=str(exc)[:200])
                break
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                if min_rating is not None:
                    try:
                        rating = item.get("rating") or 0
                        if isinstance(rating, dict):
                            rating = rating.get("average") or 0
                        if float(rating) < float(min_rating):
                            continue
                    except Exception:
                        continue
                rows.append(item)
                if len(rows) >= limit:
                    break
            if isinstance(data, dict) and data.get("meta", {}).get("has_more") is False:
                break
            page += 1
        return rows

    def build_cart(self, products: list[dict[str, Any]]) -> str:
        """Ссылка на корзину. products: [{xml_id, q}]."""
        clean: list[dict[str, Any]] = []
        for item in products[:20]:
            try:
                xml_id = int(item.get("xml_id") or item.get("id") or 0)
                quantity = float(item.get("q") or item.get("quantity") or 1)
            except Exception:
                continue
            if xml_id > 0 and 0.01 <= quantity <= 40:
                clean.append({"xml_id": xml_id, "q": quantity})
        if not clean:
            raise RuntimeError("Нечего класть в корзину")
        data = self.call_tool("vkusvill_cart_link_create", {"products": clean})
        if isinstance(data, dict):
            for key in ("url", "link", "cart_url", "result"):
                value = data.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
        if isinstance(data, str) and data.startswith("http"):
            return data
        raise RuntimeError("Сервер не вернул ссылку на корзину")

    def recipes(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        try:
            data = self.call_tool("vkusvill_recipes", {
                "q": query, "page": 1, "sort": "popularity", "id_feature_filter": 0,
                "id_cooking_time_filter": 0, "id_cooking_method_filter": 0,
                "id_complexity_filter": 0, "id_category_filter": 0,
                "id_exclude_allergens_filter": [],
            })
        except Exception:
            return []
        items = data.get("items") if isinstance(data, dict) else data
        return items[:limit] if isinstance(items, list) else []

    def discounts(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        try:
            data = self.call_tool("vkusvill_products_discount", {
                "type": "card", "sort": "rating", "page": 1, "vvonly": 0,
            })
        except Exception:
            return []
        items = data.get("items") if isinstance(data, dict) else data
        return items[:limit] if isinstance(items, list) else []

    # ---------------------------------------------------------------- ответ
    def respond(self, query: str, decision: dict[str, Any], context: str = "") -> dict[str, Any]:
        """Собрать ответ: при необходимости сходить за товарами и дать ссылку."""
        intent = str(decision.get("intent") or "chat")
        search_query = str(decision.get("search_query") or query).strip()
        min_rating = decision.get("min_rating")
        try:
            min_rating = float(min_rating) if min_rating is not None else None
        except Exception:
            min_rating = None

        products: list[dict[str, Any]] = []
        cart_url = ""
        recipes: list[dict[str, Any]] = []
        missing: list[str] = []

        needs = bool(decision.get("needs_products")) or intent in {"search", "cart", "discount"}
        if needs and not self.available():
            return {"answer": "ВкусВилл сейчас не отвечает. Не удалось проверить товары и собрать корзину.",
                    "intent": intent, "products": [], "cart_url": "", "completed": False}
        if needs:
            wanted = [str(x) for x in (decision.get("items") or []) if str(x).strip()]
            if intent == "cart" and not wanted:
                wanted = [search_query]
            if intent == "cart" and wanted:
                # Для корзины ищем каждую позицию отдельно: один общий запрос
                # возвращает похожие товары, а не разные нужные.
                chosen: list[dict[str, Any]] = []
                for name in wanted[:12]:
                    found = self.find_products(name, min_rating=min_rating, limit=1)
                    if found:
                        products.extend(found)
                        chosen.append({"xml_id": found[0].get("xml_id"), "q": 1})
                    else:
                        missing.append(name)
                if chosen and not missing:
                    try:
                        cart_url = self.build_cart(chosen)
                    except Exception as exc:
                        log_event(self.settings.root_dir, "FOOD_CART_FAILED", error=str(exc)[:200])
            elif intent == "discount":
                products = self.discounts(search_query)
            else:
                products = self.find_products(search_query, min_rating=min_rating)
                if not products and min_rating is not None:
                    # Слишком строгий рейтинг — частая причина пустой выдачи.
                    products = self.find_products(search_query, min_rating=None)
                if not products:
                    # Последняя попытка: более общий запрос вместо точного.
                    broad = str(decision.get("reply_hint") or "").strip()[:40]
                    if broad and broad.casefold() != search_query.casefold():
                        products = self.find_products(broad, min_rating=None)
        if intent == "recipe" and self.available():
            recipes = self.recipes(search_query)

        if missing:
            answer = "Не нашла во ВкусВилле: " + ", ".join(missing) + ". Полную корзину пока не собрала."
        elif intent == "cart" and not cart_url:
            answer = "Не получилось получить ссылку на корзину ВкусВилла. Корзина не подтверждена."
        elif needs and not products:
            answer = "ВкусВилл не вернул товары по этому запросу. Попробуй указать другой продукт."
        else:
            answer = self._compose(query, decision, products, recipes, cart_url, context)
        return {
            "answer": answer,
            "products": products[:8],
            "cart_url": cart_url,
            "recipes": recipes,
            "intent": intent,
            "completed": bool(cart_url) if intent == "cart" else bool(products) if needs else True,
        }

    def _compose(self, query: str, decision: dict[str, Any],
                 products: list[dict[str, Any]], recipes: list[dict[str, Any]],
                 cart_url: str, context: str) -> str:
        """Человеческий ответ. Данные подставляются, формулирует модель."""
        facts: list[str] = []
        for item in products[:8]:
            if not isinstance(item, dict):
                continue
            bits = [unescape(str(item.get("name") or "").strip())]
            price = item.get("price")
            if isinstance(price, dict):
                price = price.get("current")
            if price:
                bits.append(f"{price} ₽")
            rating = item.get("rating")
            if isinstance(rating, dict):
                rating = rating.get("average")
            if rating:
                bits.append(f"рейтинг {rating}")
            props = item.get("properties") or {}
            if isinstance(props, dict):
                kcal = props.get("calories") or props.get("kcal") or props.get("energy")
                if kcal:
                    bits.append(f"{kcal} ккал/100 г")
            facts.append(" · ".join(b for b in bits if b))
        for recipe in recipes[:3]:
            if isinstance(recipe, dict) and recipe.get("name"):
                facts.append(f"рецепт: {recipe.get('name')}")

        system = (
            "Ты — Эрви, помощник на компьютере владельца. Отвечай о еде живо и по делу, "
            "как понимающий человек, а не как каталог.\n"
            "Если есть данные о товарах — опирайся на них и называй цены и рейтинг.\n"
            "Если данных нет — спокойно поговори по существу вопроса, "
            "не выдумывая товары, цены и состав.\n"
            "Не перечисляй всё подряд: два-четыре варианта достаточно.\n"
            "Ответ короткий, до пяти предложений."
            "Говори о себе в женском роде. Не описывай действия, которых не было. "
            "Ссылка на корзину — подготовленный набор товаров, заказ ещё не оформлен и не оплачен. "
            "Каталог не подтверждает наличие по адресу: оно проверяется при оформлении."
        )
        user = f"Владелец сказал: {query}\n"
        if decision.get("reply_hint"):
            user += f"О чём ответить: {decision['reply_hint']}\n"
        if facts:
            user += "Найдено:\n" + "\n".join(f"- {f}" for f in facts) + "\n"
        else:
            user += "Товары не искали или ничего не нашлось.\n"
        if cart_url:
            user += f"Ссылка на корзину: {cart_url}\n"
        if context:
            user += f"Контекст: {context[:400]}\n"
        try:
            message = self.gateway.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=self.settings.fast_model,
                temperature=0.6, think=False,
                num_ctx=self.settings.chat_num_ctx, num_predict=420,
            )
            text = str(message.get("content") or "").strip()
        except Exception:
            text = ""
        if not text:
            text = "Нашла во ВкусВилле:\n" + "\n".join(facts[:4]) if facts else "Не получилось составить ответ про еду."
        if cart_url and cart_url not in text:
            text = f"{text}\n\nКорзина: {cart_url}"
        return text

    def status(self) -> dict[str, Any]:
        return {
            "available": bool(self._available),
            "checked_seconds_ago": round(time.monotonic() - self._checked_at, 1) if self._checked_at else None,
            "tools": sorted(TOOLS),
            "server": MCP_URL,
        }
