import hashlib
import json

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

from timetree_sync.models import SourceEvent


SYNC_APP = "timetree-google-calendar-sync"

DELETE_MISSING_THRESHOLD = 3
TOKYO = ZoneInfo("Asia/Tokyo")

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
]


@dataclass(frozen=True)
class GoogleManagedEvent:
    google_event_id: str
    source_id: str
    source_hash: str | None
    missing_count: int
    summary: str


def source_event_hash(
    event: SourceEvent,
) -> str:
    payload = {
        "title": event.title,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "source_url": event.source_url,
    }

    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def build_google_event_body(
    event: SourceEvent,
) -> dict:
    event_hash = source_event_hash(
        event
    )

    private_properties = {
        "sync_app": SYNC_APP,
        "source_id": event.source_id,
        "source_hash": event_hash,
        "missing_count": "0",
    }

    if event.source_url:
        private_properties[
            "source_url"
        ] = event.source_url

    return {
        "summary": event.title,
        "start": {
            "dateTime": event.start.isoformat(),
            "timeZone": "Asia/Tokyo",
        },
        "end": {
            "dateTime": event.end.isoformat(),
            "timeZone": "Asia/Tokyo",
        },
        "extendedProperties": {
            "private": private_properties,
        },
    }


class GoogleCalendarClient:
    def __init__(
        self,
        *,
        credentials_path: str,
        calendar_id: str,
    ) -> None:
        credentials = (
            service_account
            .Credentials
            .from_service_account_file(
                credentials_path,
                scopes=SCOPES,
            )
        )

        self.service = build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        self.calendar_id = calendar_id

    def list_managed_events(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, GoogleManagedEvent]:
        if (
            start_date is None
            and end_date is not None
        ) or (
            start_date is not None
            and end_date is None
        ):
            raise ValueError(
                "start_date and end_date must "
                "both be set or both be omitted."
            )

        if (
            start_date is not None
            and end_date is not None
            and end_date < start_date
        ):
            raise ValueError(
                "end_date must be >= start_date."
            )

        result: dict[
            str,
            GoogleManagedEvent,
        ] = {}

        list_kwargs = {
            "calendarId": self.calendar_id,
            "privateExtendedProperty": [
                f"sync_app={SYNC_APP}"
            ],
            "showDeleted": False,
            "singleEvents": True,
            "maxResults": 2500,
        }

        if (
            start_date is not None
            and end_date is not None
        ):
            time_min = datetime.combine(
                start_date,
                time.min,
                tzinfo=TOKYO,
            )

            # TimeTreeの終了日は包含なので、
            # Google側は翌日0:00をtimeMaxにする。
            time_max = datetime.combine(
                end_date + timedelta(days=1),
                time.min,
                tzinfo=TOKYO,
            )

            list_kwargs[
                "timeMin"
            ] = time_min.isoformat()

            list_kwargs[
                "timeMax"
            ] = time_max.isoformat()

        page_token = None

        while True:
            response = (
                self.service
                .events()
                .list(
                    **list_kwargs,
                    pageToken=page_token,
                )
                .execute()
            )

            for item in response.get(
                "items",
                [],
            ):
                managed = (
                    self._to_managed_event(
                        item
                    )
                )

                if (
                    managed.source_id
                    in result
                ):
                    raise RuntimeError(
                        "Duplicate source_id "
                        "found in Google Calendar: "
                        f"{managed.source_id}"
                    )

                result[
                    managed.source_id
                ] = managed

            page_token = response.get(
                "nextPageToken"
            )

            if not page_token:
                break

        return result

    def create_event(
        self,
        event: SourceEvent,
    ) -> GoogleManagedEvent:
        created = (
            self.service
            .events()
            .insert(
                calendarId=self.calendar_id,
                body=(
                    build_google_event_body(
                        event
                    )
                ),
                sendUpdates="none",
            )
            .execute()
        )

        return self._to_managed_event(
            created
        )

    def update_event(
        self,
        *,
        google_event_id: str,
        event: SourceEvent,
    ) -> GoogleManagedEvent:
        """
        TimeTree側の内容をGoogleへ反映。

        予定が再び見つかったことになるため
        missing_countも0に戻る。
        """

        updated = (
            self.service
            .events()
            .patch(
                calendarId=self.calendar_id,
                eventId=google_event_id,
                body=(
                    build_google_event_body(
                        event
                    )
                ),
                sendUpdates="none",
            )
            .execute()
        )

        return self._to_managed_event(
            updated
        )

    def delete_event(
        self,
        *,
        google_event_id: str,
    ) -> None:
        """
        Google Calendarの予定を削除する。
        """

        (
            self.service
            .events()
            .delete(
                calendarId=self.calendar_id,
                eventId=google_event_id,
            )
            .execute()
        )

    def set_missing_count(
        self,
        *,
        google_event_id: str,
        missing_count: int,
    ) -> GoogleManagedEvent:
        """
        予定本体を変更せず、
        missing_countだけ変更する。
        """

        if missing_count < 0:
            raise ValueError(
                "missing_count must be >= 0"
            )

        existing = (
            self.service
            .events()
            .get(
                calendarId=self.calendar_id,
                eventId=google_event_id,
            )
            .execute()
        )

        private = (
            existing
            .get(
                "extendedProperties",
                {},
            )
            .get(
                "private",
                {},
            )
            .copy()
        )

        private[
            "missing_count"
        ] = str(missing_count)

        updated = (
            self.service
            .events()
            .patch(
                calendarId=self.calendar_id,
                eventId=google_event_id,
                body={
                    "extendedProperties": {
                        "private": private,
                    }
                },
                sendUpdates="none",
            )
            .execute()
        )

        return self._to_managed_event(
            updated
        )

    def set_source_hash_for_test(
        self,
        *,
        google_event_id: str,
        source_hash: str,
    ) -> None:
        existing = (
            self.service
            .events()
            .get(
                calendarId=self.calendar_id,
                eventId=google_event_id,
            )
            .execute()
        )

        private = (
            existing
            .get(
                "extendedProperties",
                {},
            )
            .get(
                "private",
                {},
            )
            .copy()
        )

        private[
            "source_hash"
        ] = source_hash

        (
            self.service
            .events()
            .patch(
                calendarId=self.calendar_id,
                eventId=google_event_id,
                body={
                    "extendedProperties": {
                        "private": private,
                    }
                },
                sendUpdates="none",
            )
            .execute()
        )

    def _to_managed_event(
        self,
        item: dict,
    ) -> GoogleManagedEvent:
        private = (
            item
            .get(
                "extendedProperties",
                {},
            )
            .get(
                "private",
                {},
            )
        )

        source_id = private.get(
            "source_id"
        )

        if not source_id:
            raise RuntimeError(
                "Google event has no source_id."
            )

        raw_missing_count = (
            private.get(
                "missing_count",
                "0",
            )
        )

        try:
            missing_count = int(
                raw_missing_count
            )
        except ValueError:
            raise RuntimeError(
                "Invalid missing_count: "
                f"{raw_missing_count!r}"
            )

        return GoogleManagedEvent(
            google_event_id=item["id"],
            source_id=source_id,
            source_hash=private.get(
                "source_hash"
            ),
            missing_count=missing_count,
            summary=item.get(
                "summary",
                "",
            ),
        )
