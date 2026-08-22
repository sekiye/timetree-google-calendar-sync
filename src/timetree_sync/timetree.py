import asyncio
import calendar
import os
import re

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.async_api import Locator, Page, Request, Response, async_playwright

from timetree_sync.models import FetchResult, SourceEvent


TOKYO = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")

LOOKBACK_DAYS = 30
LOOKAHEAD_DAYS = 365

TIMETREE_SYNC_PAGE_SIZE = 300
TIMETREE_SYNC_IDLE_TIMEOUT_SECONDS = 15.0


def get_normal_sync_range(
    today: date | None = None,
) -> tuple[date, date]:
    """通常同期でTimeTree/Google双方に使う同一期間を返す。"""

    if today is None:
        today = datetime.now(
            TOKYO
        ).date()

    return (
        today - timedelta(days=LOOKBACK_DAYS),
        today + timedelta(days=LOOKAHEAD_DAYS),
    )


# ---------------------------------------------------------------------------
# Legacy URL/source-id helpers
#
# 既存Google予定との互換性のため残す。
# 旧DOM実装では繰り返し予定を <event_id>:YYYY-MM-DD で識別していた。
# JSON再構築後も同じ形式を使う。
# ---------------------------------------------------------------------------


def extract_event_id(url: str) -> str:
    match = re.search(
        r"/events/([^/?]+)",
        url,
    )

    if not match:
        raise RuntimeError(
            f"Could not extract event ID from URL: {url}"
        )

    return match.group(1)


def extract_occurrence_date(
    url: str,
) -> str | None:
    parts = urlsplit(url)
    query = parse_qs(parts.query)

    values = query.get("date")

    if not values:
        return None

    return values[0]


def build_source_id(url: str) -> str:
    event_id = extract_event_id(url)
    occurrence_date = extract_occurrence_date(
        url
    )

    if occurrence_date:
        return (
            f"{event_id}:{occurrence_date}"
        )

    return event_id


def canonical_event_url(url: str) -> str:
    """
    referer=search などは削除する。

    繰り返し予定の場合は各回を識別するため、
    date=YYYY-MM-DD だけ残す。
    """

    parts = urlsplit(url)
    query = parse_qs(parts.query)

    occurrence_date = query.get(
        "date",
        [None],
    )[0]

    if occurrence_date:
        clean_query = (
            f"date={occurrence_date}"
        )
    else:
        clean_query = ""

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            clean_query,
            "",
        )
    )


def build_event_detail_url(
    calendar_url: str,
    event_id: str,
    occurrence_date: date | None = None,
) -> str:
    """旧DOM実装が保存していた形式と同等の詳細URLを組み立てる。"""

    parts = urlsplit(calendar_url)
    base_path = parts.path.rstrip("/")
    path = (
        f"{base_path}/events/"
        f"{quote(event_id, safe='')}"
    )

    query = ""

    if occurrence_date is not None:
        query = (
            "date="
            f"{occurrence_date.isoformat()}"
        )

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            path,
            query,
            "",
        )
    )


# ---------------------------------------------------------------------------
# Login / search UI
#
# 検索UIはカレンダー選択とTARGET_NAME/期間の検証に使う。
# 予定列挙には仮想スクロールDOMを使わず、同時にブラウザが取得する
# /events/sync JSONを利用する。
# ---------------------------------------------------------------------------


async def replace_text_input(
    locator: Locator,
    value: str,
) -> None:
    await locator.click()
    await locator.select_text()
    await locator.press("Backspace")

    await locator.type(
        value,
        delay=20,
    )

    await locator.press("Tab")


async def login(
    page: Page,
    email: str,
    password: str,
) -> None:
    await page.goto(
        "https://timetreeapp.com/signin",
        wait_until="domcontentloaded",
    )

    # Do not depend on the TimeTree UI language.
    # GitHub Actions runners may render the sign-in page in English.
    email_input = page.locator(
        'input[type="email"]'
    ).first

    password_input = page.locator(
        'input[type="password"]'
    ).first

    await email_input.wait_for(
        state="visible",
        timeout=15000,
    )

    await password_input.wait_for(
        state="visible",
        timeout=15000,
    )

    await email_input.fill(email)
    await password_input.fill(password)

    # Submitting with Enter avoids depending on the localized
    # login button text ("ログイン", "Log In", etc.).
    await password_input.press("Enter")

    await password_input.wait_for(
        state="hidden",
        timeout=20000,
    )


async def open_search(
    page: Page,
    calendar_url: str,
    calendar_name: str,
    target_name: str,
) -> str:
    """対象カレンダーを開いてTARGET_NAME検索を開始する。"""

    await page.goto(
        calendar_url,
        wait_until="domcontentloaded",
    )

    await page.wait_for_timeout(2000)

    search_button = page.get_by_role(
        "button",
        name="Enter keywords to search",
        exact=True,
    )

    await search_button.wait_for(
        state="visible",
        timeout=10000,
    )

    await search_button.click()

    search_input = page.locator(
        'input[name="search-field"]'
    )

    await search_input.wait_for(
        state="visible",
        timeout=10000,
    )

    await search_input.fill(
        target_name
    )

    await search_input.press(
        "Enter"
    )

    await page.wait_for_url(
        "**/calendars/search**",
        timeout=15000,
    )

    result_search_input = page.locator(
        'input[name="search-query-input"]'
    )

    await result_search_input.wait_for(
        state="visible",
        timeout=15000,
    )

    calendar_input = page.locator(
        'input[name="selectCalendar"]'
    )

    await calendar_input.wait_for(
        state="visible",
        timeout=10000,
    )

    actual_calendar = ""

    # TimeTreeの検索画面では selectCalendar が先に visible になり、
    # 選択済みカレンダー名の value 反映が少し遅れることがある。
    # 空文字を即エラーにはせず、短時間だけ値の反映を待つ。
    for _ in range(20):
        actual_calendar = (
            await calendar_input.input_value()
        ).strip()

        if actual_calendar:
            break

        await page.wait_for_timeout(250)

    # calendar_url から対象カレンダーへ直接遷移しているため、
    # value が最後まで空の場合は表示上の未反映として許容する。
    # 値が取得できた場合の不一致だけを安全エラーにする。
    if (
        actual_calendar
        and actual_calendar != calendar_name
    ):
        raise RuntimeError(
            "Unexpected TimeTree calendar. "
            f"expected={calendar_name!r}, "
            f"actual={actual_calendar!r}"
        )

    if not actual_calendar:
        print(
            "TIMETREE CALENDAR NAME: "
            "not exposed by search input; "
            "continuing with calendar URL"
        )

    return page.url


async def set_search_range(
    page: Page,
    start_date: date,
    end_date: date,
) -> None:
    start_text = start_date.strftime(
        "%b %-d, %Y"
    )

    end_text = end_date.strftime(
        "%b %-d, %Y"
    )

    start_input = page.locator(
        'input[name="searchStartDate"]'
    )

    end_input = page.locator(
        'input[name="searchEndDate"]'
    )

    await start_input.wait_for(
        state="visible",
        timeout=10000,
    )

    await end_input.wait_for(
        state="visible",
        timeout=10000,
    )

    # 開始日変更時に終了日が自動変更されるため、開始日を先に設定する。
    await replace_text_input(
        start_input,
        start_text,
    )

    await page.wait_for_timeout(500)

    end_input = page.locator(
        'input[name="searchEndDate"]'
    )

    await end_input.wait_for(
        state="visible",
        timeout=10000,
    )

    await replace_text_input(
        end_input,
        end_text,
    )

    await page.wait_for_timeout(1000)

    start_input = page.locator(
        'input[name="searchStartDate"]'
    )

    end_input = page.locator(
        'input[name="searchEndDate"]'
    )

    actual_start = (
        await start_input.input_value()
    )

    actual_end = (
        await end_input.input_value()
    )

    if actual_start != start_text:
        raise RuntimeError(
            "Failed to set search start date. "
            f"expected={start_text!r}, "
            f"actual={actual_start!r}"
        )

    if actual_end != end_text:
        raise RuntimeError(
            "Failed to set search end date. "
            f"expected={end_text!r}, "
            f"actual={actual_end!r}"
        )


# ---------------------------------------------------------------------------
# /events/sync capture
# ---------------------------------------------------------------------------


@dataclass
class _SyncCapture:
    pages: list[dict] = field(
        default_factory=list
    )
    events: list[dict] = field(
        default_factory=list
    )
    errors: list[str] = field(
        default_factory=list
    )
    terminal_seen: bool = False


def _is_events_sync_url(
    url: str,
) -> bool:
    return urlsplit(
        url
    ).path.endswith(
        "/events/sync"
    )


def _request_since(
    url: str,
) -> str | None:
    query = parse_qs(
        urlsplit(url).query
    )

    values = query.get("since")

    if not values:
        return None

    return values[0]


async def _consume_sync_responses(
    queue: asyncio.Queue[Response],
    capture: _SyncCapture,
    terminal_event: asyncio.Event,
) -> None:
    while True:
        response = await queue.get()

        try:
            if response.status != 200:
                capture.errors.append(
                    "events/sync HTTP status "
                    f"{response.status}"
                )
                continue

            try:
                data = await response.json()
            except Exception as exc:
                capture.errors.append(
                    "events/sync JSON decode failed: "
                    f"{type(exc).__name__}"
                )
                continue

            if not isinstance(data, dict):
                capture.errors.append(
                    "events/sync response was not an object"
                )
                continue

            page_events = data.get(
                "events"
            )

            if not isinstance(
                page_events,
                list,
            ):
                capture.errors.append(
                    "events/sync 'events' was not a list"
                )
                continue

            if len(page_events) > TIMETREE_SYNC_PAGE_SIZE:
                capture.errors.append(
                    "events/sync page exceeded expected "
                    f"size {TIMETREE_SYNC_PAGE_SIZE}: "
                    f"{len(page_events)}"
                )
                continue

            request_since = _request_since(
                response.url
            )
            response_since = data.get(
                "since"
            )

            # 新しいfull-sync chainが始まった場合は、古い未完chainを捨てる。
            # 通常は最初の1回だけsinceなしで始まる。
            if (
                request_since is None
                and capture.pages
                and not capture.terminal_seen
            ):
                print(
                    "TIMETREE SYNC: new chain detected; "
                    "resetting incomplete capture"
                )
                capture.pages.clear()
                capture.events.clear()
                capture.errors.clear()

            if capture.pages:
                expected_since = capture.pages[-1].get(
                    "response_since"
                )

                if (
                    expected_since is None
                    or request_since
                    != str(expected_since)
                ):
                    capture.errors.append(
                        "events/sync pagination chain mismatch: "
                        f"expected since={expected_since!r}, "
                        f"request since={request_since!r}"
                    )

            elif request_since is not None:
                capture.errors.append(
                    "events/sync capture started mid-chain: "
                    f"since={request_since!r}"
                )

            page_number = (
                len(capture.pages) + 1
            )

            capture.pages.append(
                {
                    "request_since": request_since,
                    "response_since": response_since,
                    "count": len(page_events),
                }
            )

            capture.events.extend(
                page_events
            )

            print(
                "TIMETREE SYNC PAGE:",
                page_number,
                "events=",
                len(page_events),
            )

            if (
                len(page_events)
                < TIMETREE_SYNC_PAGE_SIZE
            ):
                capture.terminal_seen = True
                terminal_event.set()
                return

            if response_since is None:
                capture.errors.append(
                    "events/sync full page had no 'since' cursor"
                )

        finally:
            queue.task_done()


async def capture_all_timetree_events(
    page: Page,
    *,
    calendar_url: str,
    calendar_name: str,
    target_name: str,
    start_date: date,
    end_date: date,
) -> tuple[list[dict], bool, str | None]:
    """
    TimeTree Webが通常操作中に取得する /events/sync を受動的に収集する。

    完了条件:
      - pagination chainが正常
      - 最終ページのevents件数が300未満
      - events/sync request failureなし
    """

    queue: asyncio.Queue[Response] = asyncio.Queue()
    capture = _SyncCapture()
    terminal_event = asyncio.Event()
    failed_requests: list[str] = []

    def on_response(
        response: Response,
    ) -> None:
        if _is_events_sync_url(
            response.url
        ):
            queue.put_nowait(
                response
            )

    def on_request_failed(
        request: Request,
    ) -> None:
        if _is_events_sync_url(
            request.url
        ):
            failed_requests.append(
                request.url
            )

    page.on(
        "response",
        on_response,
    )
    page.on(
        "requestfailed",
        on_request_failed,
    )

    consumer = asyncio.create_task(
        _consume_sync_responses(
            queue,
            capture,
            terminal_event,
        )
    )

    try:
        await open_search(
            page,
            calendar_url,
            calendar_name,
            target_name,
        )

        await set_search_range(
            page,
            start_date,
            end_date,
        )

        try:
            await asyncio.wait_for(
                terminal_event.wait(),
                timeout=(
                    TIMETREE_SYNC_IDLE_TIMEOUT_SECONDS
                    * 5
                ),
            )
        except asyncio.TimeoutError:
            capture.errors.append(
                "Timed out before terminal events/sync page "
                f"(< {TIMETREE_SYNC_PAGE_SIZE} events)"
            )

        # queue済みレスポンスの解析完了を待つ。
        try:
            await asyncio.wait_for(
                queue.join(),
                timeout=TIMETREE_SYNC_IDLE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            capture.errors.append(
                "Timed out while processing events/sync responses"
            )

    finally:
        if not consumer.done():
            consumer.cancel()

            try:
                await consumer
            except asyncio.CancelledError:
                pass

        page.remove_listener(
            "response",
            on_response,
        )
        page.remove_listener(
            "requestfailed",
            on_request_failed,
        )

    if failed_requests:
        capture.errors.append(
            "One or more events/sync requests failed"
        )

    complete = (
        capture.terminal_seen
        and not capture.errors
        and bool(capture.pages)
    )

    reason = None

    if not complete:
        reason = "; ".join(
            dict.fromkeys(
                capture.errors
            )
        ) or "events/sync capture was incomplete"

    print(
        "TIMETREE SYNC RAW EVENTS:",
        len(capture.events),
    )
    print(
        "TIMETREE SYNC PAGES:",
        len(capture.pages),
    )
    print(
        "TIMETREE SYNC COMPLETE:",
        complete,
    )

    if reason:
        print(
            "TIMETREE SYNC INCOMPLETE REASON:",
            reason,
        )

    return (
        capture.events,
        complete,
        reason,
    )


# ---------------------------------------------------------------------------
# JSON -> SourceEvent reconstruction
# ---------------------------------------------------------------------------


def _event_key(
    event: dict,
) -> str | None:
    uuid = event.get("uuid")

    if isinstance(uuid, str) and uuid:
        return uuid

    event_id = event.get("id")

    if isinstance(event_id, str) and event_id:
        return event_id

    return None


def _is_active_event(
    event: dict,
) -> bool:
    return not bool(
        event.get("deactivated_at")
    )


def _event_title(
    event: dict,
) -> str:
    title = event.get("title")

    if isinstance(title, str):
        return title.strip()

    return ""


def _ms_to_tokyo(
    value,
) -> datetime | None:
    if not isinstance(
        value,
        (int, float),
    ):
        return None

    try:
        return datetime.fromtimestamp(
            value / 1000,
            tz=TOKYO,
        )
    except (OverflowError, OSError, ValueError):
        return None


def _event_datetimes(
    event: dict,
) -> tuple[datetime, datetime] | None:
    if event.get("all_day"):
        return None

    start = _ms_to_tokyo(
        event.get("start_at")
    )
    end = _ms_to_tokyo(
        event.get("end_at")
    )

    if start is None or end is None:
        return None

    if end <= start:
        return None

    return start, end


def _dedupe_events(
    raw_events: list[dict],
) -> dict[str, dict]:
    """同じuuid/idが複数ページに出た場合はupdated_atが新しい方を残す。"""

    result: dict[str, dict] = {}

    for event in raw_events:
        if not isinstance(event, dict):
            continue

        key = _event_key(event)

        if key is None:
            continue

        existing = result.get(key)

        if existing is None:
            result[key] = event
            continue

        current_updated = event.get(
            "updated_at",
            0,
        )
        existing_updated = existing.get(
            "updated_at",
            0,
        )

        if (
            isinstance(current_updated, (int, float))
            and isinstance(existing_updated, (int, float))
            and current_updated >= existing_updated
        ):
            result[key] = event

    return result


def _parse_exdate(
    raw: str,
) -> datetime:
    # 実測: EXDATE:20260807T063000Z
    if raw.endswith("Z"):
        value = datetime.strptime(
            raw,
            "%Y%m%dT%H%M%SZ",
        ).replace(
            tzinfo=UTC
        )

        return value.astimezone(
            TOKYO
        )

    # 将来TimeTree側がlocal日時を返した場合にも対応する。
    if "T" in raw:
        value = datetime.strptime(
            raw,
            "%Y%m%dT%H%M%S",
        )

        return value.replace(
            tzinfo=TOKYO
        )

    value = datetime.strptime(
        raw,
        "%Y%m%d",
    )

    return value.replace(
        tzinfo=TOKYO
    )


def _parse_rrule(
    line: str,
) -> dict[str, str]:
    if not line.startswith("RRULE:"):
        raise ValueError(
            "Not an RRULE entry"
        )

    result: dict[str, str] = {}

    for piece in line[len("RRULE:"):].split(";"):
        if "=" not in piece:
            raise ValueError(
                f"Malformed RRULE piece: {piece!r}"
            )

        key, value = piece.split(
            "=",
            1,
        )
        result[key.upper()] = value.upper()

    return result


_WEEKDAYS = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}


def _parse_until_date(
    raw: str | None,
) -> date | None:
    if not raw:
        return None

    # 実測はYYYYMMDD。UTC datetime形式も日付境界として扱う。
    for fmt in (
        "%Y%m%d",
        "%Y%m%dT%H%M%SZ",
        "%Y%m%dT%H%M%S",
    ):
        try:
            return datetime.strptime(
                raw,
                fmt,
            ).date()
        except ValueError:
            pass

    raise ValueError(
        f"Unsupported RRULE UNTIL: {raw!r}"
    )


def _month_index(
    year: int,
    month: int,
) -> int:
    return year * 12 + (month - 1)


def _iter_months(
    start: date,
    end: date,
):
    year = start.year
    month = start.month

    while date(
        year,
        month,
        1,
    ) <= end:
        yield year, month

        if month == 12:
            year += 1
            month = 1
        else:
            month += 1


def _nth_weekday_of_month(
    year: int,
    month: int,
    weekday: int,
    ordinal: int,
) -> date | None:
    weeks = calendar.monthcalendar(
        year,
        month,
    )

    days = [
        week[weekday]
        for week in weeks
        if week[weekday] != 0
    ]

    if ordinal > 0:
        index = ordinal - 1
    else:
        index = ordinal

    try:
        day = days[index]
    except IndexError:
        return None

    return date(
        year,
        month,
        day,
    )


def _rule_dates(
    rule: dict[str, str],
    *,
    master_date: date,
    range_start: date,
    range_end: date,
) -> list[date]:
    """
    TimeTreeで実測したRFC5545 RRULEの主要形を標準ライブラリだけで展開する。

    対応:
      DAILY
      WEEKLY (+ INTERVAL/BYDAY/UNTIL)
      MONTHLY (+ INTERVAL/BYDAY ordinal/BYMONTHDAY/UNTIL)
      YEARLY (+ INTERVAL/BYMONTH/BYMONTHDAY/UNTIL)

    未対応キーは推測で処理せずValueErrorにしてcomplete=Falseへ落とす。
    """

    freq = rule.get("FREQ")

    if not freq:
        raise ValueError(
            "RRULE has no FREQ"
        )

    try:
        interval = int(
            rule.get(
                "INTERVAL",
                "1",
            )
        )
    except ValueError as exc:
        raise ValueError(
            "Invalid RRULE INTERVAL"
        ) from exc

    if interval <= 0:
        raise ValueError(
            "RRULE INTERVAL must be > 0"
        )

    until = _parse_until_date(
        rule.get("UNTIL")
    )

    effective_end = range_end

    if until is not None:
        effective_end = min(
            effective_end,
            until,
        )

    if effective_end < range_start:
        return []

    # COUNTは系列開始から数える必要がある。
    count_limit = None

    if "COUNT" in rule:
        try:
            count_limit = int(
                rule["COUNT"]
            )
        except ValueError as exc:
            raise ValueError(
                "Invalid RRULE COUNT"
            ) from exc

        if count_limit <= 0:
            return []

    result: list[date] = []
    generated_total = 0

    def add_candidate(
        candidate: date,
    ) -> bool:
        nonlocal generated_total

        if candidate < master_date:
            return True

        if until is not None and candidate > until:
            return False

        generated_total += 1

        if (
            count_limit is not None
            and generated_total > count_limit
        ):
            return False

        if (
            range_start
            <= candidate
            <= range_end
        ):
            result.append(candidate)

        return True

    common_keys = {
        "FREQ",
        "INTERVAL",
        "UNTIL",
        "COUNT",
    }

    if freq == "DAILY":
        unsupported = (
            set(rule)
            - common_keys
        )

        if unsupported:
            raise ValueError(
                "Unsupported DAILY RRULE keys: "
                f"{sorted(unsupported)}"
            )

        current = master_date

        while current <= effective_end:
            if not add_candidate(current):
                break

            current += timedelta(
                days=interval
            )

        return result

    if freq == "WEEKLY":
        allowed = (
            common_keys
            | {"BYDAY"}
        )
        unsupported = (
            set(rule)
            - allowed
        )

        if unsupported:
            raise ValueError(
                "Unsupported WEEKLY RRULE keys: "
                f"{sorted(unsupported)}"
            )

        raw_days = rule.get("BYDAY")

        if raw_days:
            weekdays = []

            for token in raw_days.split(","):
                # WEEKLYのBYDAY ordinalはここでは未対応。
                if token not in _WEEKDAYS:
                    raise ValueError(
                        "Unsupported WEEKLY BYDAY: "
                        f"{token!r}"
                    )

                weekdays.append(
                    _WEEKDAYS[token]
                )
        else:
            weekdays = [
                master_date.weekday()
            ]

        week_zero = (
            master_date
            - timedelta(
                days=master_date.weekday()
            )
        )
        current_week = week_zero

        while current_week <= effective_end:
            for weekday in sorted(
                weekdays
            ):
                candidate = (
                    current_week
                    + timedelta(days=weekday)
                )

                if candidate > effective_end:
                    continue

                if not add_candidate(candidate):
                    return result

            current_week += timedelta(
                weeks=interval
            )

        return result

    if freq == "MONTHLY":
        allowed = (
            common_keys
            | {"BYDAY", "BYMONTHDAY"}
        )
        unsupported = (
            set(rule)
            - allowed
        )

        if unsupported:
            raise ValueError(
                "Unsupported MONTHLY RRULE keys: "
                f"{sorted(unsupported)}"
            )

        if (
            "BYDAY" in rule
            and "BYMONTHDAY" in rule
        ):
            raise ValueError(
                "MONTHLY RRULE with both BYDAY and BYMONTHDAY "
                "is not supported"
            )

        master_month_index = _month_index(
            master_date.year,
            master_date.month,
        )

        # COUNTのため、master月から展開する。
        for year, month in _iter_months(
            date(
                master_date.year,
                master_date.month,
                1,
            ),
            effective_end,
        ):
            diff = (
                _month_index(year, month)
                - master_month_index
            )

            if diff % interval != 0:
                continue

            candidates: list[date] = []

            if "BYDAY" in rule:
                for token in rule[
                    "BYDAY"
                ].split(","):
                    match = re.fullmatch(
                        r"(-?[1-5])"
                        r"(MO|TU|WE|TH|FR|SA|SU)",
                        token,
                    )

                    if not match:
                        raise ValueError(
                            "Unsupported MONTHLY BYDAY: "
                            f"{token!r}"
                        )

                    candidate = _nth_weekday_of_month(
                        year,
                        month,
                        _WEEKDAYS[
                            match.group(2)
                        ],
                        int(match.group(1)),
                    )

                    if candidate is not None:
                        candidates.append(
                            candidate
                        )

            elif "BYMONTHDAY" in rule:
                last_day = calendar.monthrange(
                    year,
                    month,
                )[1]

                for raw_day in rule[
                    "BYMONTHDAY"
                ].split(","):
                    day = int(raw_day)

                    if day < 0:
                        day = (
                            last_day + day + 1
                        )

                    if 1 <= day <= last_day:
                        candidates.append(
                            date(
                                year,
                                month,
                                day,
                            )
                        )

            else:
                last_day = calendar.monthrange(
                    year,
                    month,
                )[1]

                if master_date.day <= last_day:
                    candidates.append(
                        date(
                            year,
                            month,
                            master_date.day,
                        )
                    )

            for candidate in sorted(
                set(candidates)
            ):
                if not add_candidate(candidate):
                    return result

        return result

    if freq == "YEARLY":
        allowed = (
            common_keys
            | {"BYMONTH", "BYMONTHDAY"}
        )
        unsupported = (
            set(rule)
            - allowed
        )

        if unsupported:
            raise ValueError(
                "Unsupported YEARLY RRULE keys: "
                f"{sorted(unsupported)}"
            )

        months = [
            int(value)
            for value in rule.get(
                "BYMONTH",
                str(master_date.month),
            ).split(",")
        ]
        month_days = [
            int(value)
            for value in rule.get(
                "BYMONTHDAY",
                str(master_date.day),
            ).split(",")
        ]

        year = master_date.year

        while date(
            year,
            1,
            1,
        ) <= effective_end:
            if (
                (year - master_date.year)
                % interval
                != 0
            ):
                year += 1
                continue

            for month in sorted(
                set(months)
            ):
                if not 1 <= month <= 12:
                    raise ValueError(
                        "Invalid YEARLY BYMONTH"
                    )

                last_day = calendar.monthrange(
                    year,
                    month,
                )[1]

                for raw_day in month_days:
                    day = raw_day

                    if day < 0:
                        day = (
                            last_day + day + 1
                        )

                    if not 1 <= day <= last_day:
                        continue

                    candidate = date(
                        year,
                        month,
                        day,
                    )

                    if not add_candidate(candidate):
                        return result

            year += 1

        return result

    raise ValueError(
        f"Unsupported RRULE FREQ: {freq!r}"
    )


def _expand_master_occurrences(
    master: dict,
    *,
    start_date: date,
    end_date: date,
) -> list[datetime]:
    datetimes = _event_datetimes(
        master
    )

    if datetimes is None:
        return []

    master_start, _ = datetimes
    recurrences = master.get(
        "recurrences"
    )

    if not isinstance(
        recurrences,
        list,
    ) or not recurrences:
        raise ValueError(
            "Recurring master has no recurrence entries"
        )

    rules: list[dict[str, str]] = []
    exdates: set[datetime] = set()

    for value in recurrences:
        if not isinstance(value, str):
            raise ValueError(
                "Non-string recurrence entry"
            )

        if value.startswith("RRULE:"):
            rules.append(
                _parse_rrule(value)
            )
            continue

        if value.startswith("EXDATE:"):
            exdates.add(
                _parse_exdate(
                    value[len("EXDATE:"):]
                ).replace(
                    microsecond=0
                )
            )
            continue

        raise ValueError(
            "Unsupported recurrence entry: "
            f"{value.split(':', 1)[0]!r}"
        )

    if not rules:
        raise ValueError(
            "Recurring master has no RRULE"
        )

    generated_dates: set[date] = set()

    for rule in rules:
        generated_dates.update(
            _rule_dates(
                rule,
                master_date=master_start.date(),
                range_start=start_date,
                range_end=end_date,
            )
        )

    generated = [
        datetime.combine(
            occurrence_date,
            master_start.timetz(),
        ).replace(
            microsecond=0
        )
        for occurrence_date in generated_dates
    ]

    return sorted(
        value
        for value in generated
        if value not in exdates
    )


def _source_id_for_recurring(
    series_id: str,
    occurrence_start: datetime,
) -> str:
    # 旧DOM実装 build_source_id() との互換形式。
    return (
        f"{series_id}:"
        f"{occurrence_start.date().isoformat()}"
    )


def reconstruct_source_events(
    raw_events: list[dict],
    *,
    calendar_url: str,
    target_name: str,
    start_date: date,
    end_date: date,
) -> tuple[list[SourceEvent], bool, str | None]:
    unique = _dedupe_events(
        raw_events
    )

    active = {
        key: event
        for key, event in unique.items()
        if _is_active_event(event)
    }

    source_by_id: dict[
        str,
        SourceEvent,
    ] = {}

    errors: list[str] = []

    # 1) TARGET_NAMEを含む系列masterをRRULE展開する。
    target_master_ids: set[str] = set()

    for key, event in active.items():
        recurring_uuid = event.get(
            "recurring_uuid"
        )
        recurrences = event.get(
            "recurrences"
        )

        is_master = (
            not recurring_uuid
            and isinstance(recurrences, list)
            and bool(recurrences)
        )

        if not is_master:
            continue

        if target_name not in _event_title(event):
            continue

        target_master_ids.add(key)

        datetimes = _event_datetimes(
            event
        )

        if datetimes is None:
            print(
                "SKIP ALL-DAY RECURRING MASTER:",
                key,
            )
            continue

        master_start, master_end = datetimes
        duration = (
            master_end - master_start
        )

        try:
            starts = _expand_master_occurrences(
                event,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:
            errors.append(
                "Could not expand recurring master "
                f"{key}: {type(exc).__name__}: {exc}"
            )
            continue

        title = _event_title(
            event
        )

        for occurrence_start in starts:
            source_id = _source_id_for_recurring(
                key,
                occurrence_start,
            )

            source_by_id[source_id] = SourceEvent(
                source_id=source_id,
                title=title,
                start=occurrence_start,
                end=(
                    occurrence_start
                    + duration
                ),
                source_url=build_event_detail_url(
                    calendar_url,
                    key,
                    occurrence_start.date(),
                ),
            )

    # 2) recurring child/exceptionを全件見る。
    #    TARGET_NAMEが消えたchildは追加しない。
    #    EXDATE済みbaseも上で既に除外されているため、キャンセル/改名に対応できる。
    for key, event in active.items():
        recurring_uuid = event.get(
            "recurring_uuid"
        )

        if not isinstance(
            recurring_uuid,
            str,
        ) or not recurring_uuid:
            continue

        if target_name not in _event_title(event):
            continue

        datetimes = _event_datetimes(
            event
        )

        if datetimes is None:
            continue

        start, end = datetimes

        if not (
            start_date
            <= start.date()
            <= end_date
        ):
            continue

        # 旧DOM実装との互換性:
        # 通常の繰り返し occurrence は series_id:date だが、
        # 個別変更された child/exception を検索結果から開くと
        # detail URL は child 自身の /events/<child_uuid> になり、
        # date query も付かない。そのため既存Google予定では
        # child UUID単体が source_id として保存されている。
        source_id = key

        child = SourceEvent(
            source_id=source_id,
            title=_event_title(event),
            start=start,
            end=end,
            source_url=build_event_detail_url(
                calendar_url,
                key,
            ),
        )

        existing = source_by_id.get(
            source_id
        )

        if (
            existing is not None
            and existing != child
        ):
            errors.append(
                "Recurring child source_id collision: "
                f"{source_id}"
            )
            continue

        source_by_id[
            source_id
        ] = child

    # 3) 単発予定。
    for key, event in active.items():
        recurring_uuid = event.get(
            "recurring_uuid"
        )
        recurrences = event.get(
            "recurrences"
        )

        if recurring_uuid:
            continue

        if (
            isinstance(recurrences, list)
            and recurrences
        ):
            # recurring masterは上で処理済み。
            continue

        title = _event_title(
            event
        )

        if target_name not in title:
            continue

        datetimes = _event_datetimes(
            event
        )

        if datetimes is None:
            continue

        start, end = datetimes

        if not (
            start_date
            <= start.date()
            <= end_date
        ):
            continue

        source_event = SourceEvent(
            source_id=key,
            title=title,
            start=start,
            end=end,
            source_url=build_event_detail_url(
                calendar_url,
                key,
            ),
        )

        existing = source_by_id.get(
            key
        )

        if (
            existing is not None
            and existing != source_event
        ):
            errors.append(
                "Single event source_id collision: "
                f"{key}"
            )
            continue

        source_by_id[
            key
        ] = source_event

    events = sorted(
        source_by_id.values(),
        key=lambda event: (
            event.start,
            event.source_id,
        ),
    )

    complete = not errors
    reason = None

    if errors:
        reason = "; ".join(
            errors
        )

    return events, complete, reason


def validate_events(
    events: list[SourceEvent],
    target_name: str,
) -> None:
    source_ids = [
        event.source_id
        for event in events
    ]

    unique_ids = set(
        source_ids
    )

    if len(source_ids) != len(unique_ids):
        duplicates = sorted(
            {
                source_id
                for source_id in source_ids
                if source_ids.count(source_id) > 1
            }
        )

        raise RuntimeError(
            "Duplicate source IDs detected: "
            f"{duplicates}"
        )

    invalid_titles = [
        event.title
        for event in events
        if target_name not in event.title
    ]

    if invalid_titles:
        raise RuntimeError(
            "TARGET_NAME filter validation failed: "
            f"{invalid_titles}"
        )


async def fetch_timetree_events(
    *,
    email: str,
    password: str,
    calendar_url: str,
    calendar_name: str,
    target_name: str,
    headless: bool = False,
    start_date: date | None = None,
    end_date: date | None = None,
) -> FetchResult:
    """
    TimeTree Webの /events/sync を完全取得し、検索結果相当を再構築する。

    complete=False の場合、呼び出し側はmissing_count/DELETEを行ってはいけない。
    """

    if (
        start_date is None
        and end_date is None
    ):
        (
            start_date,
            end_date,
        ) = get_normal_sync_range()

    elif (
        start_date is None
        or end_date is None
    ):
        raise ValueError(
            "start_date and end_date must "
            "both be set or both be omitted."
        )

    if end_date < start_date:
        raise ValueError(
            "end_date must be >= start_date."
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless
        )

        page = await browser.new_page(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        try:
            print("LOGIN...")

            await login(
                page,
                email,
                password,
            )

            print("LOGIN: OK")
            print("SEARCH + EVENTS/SYNC...")
            print(
                "SYNC RANGE:",
                start_date,
                "->",
                end_date,
            )

            (
                raw_events,
                transport_complete,
                transport_reason,
            ) = await capture_all_timetree_events(
                page,
                calendar_url=calendar_url,
                calendar_name=calendar_name,
                target_name=target_name,
                start_date=start_date,
                end_date=end_date,
            )

            (
                events,
                reconstruction_complete,
                reconstruction_reason,
            ) = reconstruct_source_events(
                raw_events,
                calendar_url=calendar_url,
                target_name=target_name,
                start_date=start_date,
                end_date=end_date,
            )

            validate_events(
                events,
                target_name,
            )

            complete = (
                transport_complete
                and reconstruction_complete
            )

            reasons = [
                reason
                for reason in (
                    transport_reason,
                    reconstruction_reason,
                )
                if reason
            ]

            incomplete_reason = (
                "; ".join(reasons)
                if reasons
                else None
            )

            print(
                "TIMETREE RECONSTRUCTED EVENTS:",
                len(events),
            )
            print(
                "TIMETREE FETCH COMPLETE:",
                complete,
            )

            if incomplete_reason:
                print(
                    "TIMETREE FETCH INCOMPLETE REASON:",
                    incomplete_reason,
                )

            return FetchResult(
                events=events,
                complete=complete,
                incomplete_reason=incomplete_reason,
            )

        finally:
            await browser.close()


async def main() -> None:
    load_dotenv()

    result = await fetch_timetree_events(
        email=os.environ[
            "TIMETREE_EMAIL"
        ],
        password=os.environ[
            "TIMETREE_PASSWORD"
        ],
        calendar_url=os.environ[
            "TIMETREE_CALENDAR_URL"
        ],
        calendar_name=os.environ[
            "TIMETREE_CALENDAR_NAME"
        ],
        target_name=os.environ[
            "TARGET_NAME"
        ],
        headless=False,
    )

    print("\n=== TIMETREE EVENTS ===")
    print(
        "TOTAL:",
        len(result.events),
    )
    print(
        "COMPLETE:",
        result.complete,
    )

    if result.incomplete_reason:
        print(
            "INCOMPLETE REASON:",
            result.incomplete_reason,
        )

    for index, event in enumerate(
        result.events,
        start=1,
    ):
        print(
            f"\nEVENT {index}"
        )
        print(
            "source_id:",
            event.source_id,
        )
        print(
            "title:",
            repr(event.title),
        )
        print(
            "start:",
            event.start.isoformat(),
        )
        print(
            "end:",
            event.end.isoformat(),
        )
        print(
            "source_url:",
            event.source_url,
        )


if __name__ == "__main__":
    asyncio.run(main())