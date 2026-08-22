import asyncio
import os

from dotenv import load_dotenv

from timetree_sync.google_calendar import (
    DELETE_MISSING_THRESHOLD,
    GoogleCalendarClient,
    source_event_hash,
)
from timetree_sync.timetree import (
    fetch_timetree_events,
    get_normal_sync_range,
)


async def main() -> None:
    load_dotenv()

    print(
        "=== TIMETREE -> GOOGLE CALENDAR DRY RUN ==="
    )

    (
        sync_start_date,
        sync_end_date,
    ) = get_normal_sync_range()

    print(
        "\nSYNC RANGE:",
        sync_start_date,
        "->",
        sync_end_date,
    )

    print(
        "\n1. Fetching TimeTree events..."
    )

    fetch_result = (
        await fetch_timetree_events(
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
            start_date=sync_start_date,
            end_date=sync_end_date,
        )
    )

    source_events = fetch_result.events

    print(
        "\nTIMETREE EVENTS:",
        len(source_events),
    )

    print(
        "TIMETREE FETCH COMPLETE:",
        fetch_result.complete,
    )

    if fetch_result.incomplete_reason:
        print(
            "TIMETREE FETCH INCOMPLETE REASON:",
            fetch_result.incomplete_reason,
        )

    print(
        "\n2. Reading managed "
        "Google Calendar events..."
    )

    google = GoogleCalendarClient(
        credentials_path=os.environ[
            "GOOGLE_SERVICE_ACCOUNT_JSON"
        ],
        calendar_id=os.environ[
            "GOOGLE_CALENDAR_ID"
        ],
    )

    google_events = (
        google.list_managed_events(
            start_date=sync_start_date,
            end_date=sync_end_date,
        )
    )

    print(
        "GOOGLE MANAGED EVENTS:",
        len(google_events),
    )

    current_source_ids = {
        event.source_id
        for event in source_events
    }

    matching_source_ids = (
        current_source_ids
        & set(google_events.keys())
    )

    print(
        "SOURCE ID MATCHES:",
        len(matching_source_ids),
    )

    if (
        google_events
        and source_events
        and not matching_source_ids
    ):
        print(
            "WARNING: NO SOURCE ID MATCHES. "
            "Do not run sync_create_update.py yet; "
            "source_id migration/compatibility must be checked."
        )

    # ==========================================
    # Compare
    # ==========================================

    create_events = []
    update_events = []
    unchanged_events = []

    source_ids = set()

    for event in source_events:
        source_ids.add(
            event.source_id
        )

        expected_hash = (
            source_event_hash(event)
        )

        existing = google_events.get(
            event.source_id
        )

        if existing is None:
            create_events.append(
                event
            )

            continue

        if (
            existing.source_hash
            != expected_hash
        ):
            update_events.append(
                (
                    event,
                    existing,
                )
            )

            continue

        unchanged_events.append(
            event
        )

    missing_events = [
        google_event
        for source_id, google_event
        in google_events.items()
        if source_id not in source_ids
    ]

    would_increment_missing = []
    would_delete = []

    if fetch_result.complete:
        for google_event in missing_events:
            new_missing_count = (
                google_event.missing_count + 1
            )

            if (
                new_missing_count
                >= DELETE_MISSING_THRESHOLD
            ):
                would_delete.append(
                    (
                        google_event,
                        new_missing_count,
                    )
                )
            else:
                would_increment_missing.append(
                    (
                        google_event,
                        new_missing_count,
                    )
                )

    # ==========================================
    # Report
    # ==========================================

    print(
        "\n=============================="
    )

    print(
        "DRY RUN RESULT"
    )

    print(
        "=============================="
    )

    print(
        "CREATE:",
        len(create_events),
    )

    print(
        "UPDATE:",
        len(update_events),
    )

    print(
        "UNCHANGED:",
        len(unchanged_events),
    )

    print(
        "MISSING FROM TIMETREE:",
        len(missing_events),
    )

    print(
        "WOULD INCREMENT MISSING:",
        len(would_increment_missing),
    )

    print(
        "WOULD DELETE:",
        len(would_delete),
    )

    if create_events:
        print(
            "\n=== CREATE ==="
        )

        for event in create_events:
            print(
                "+",
                event.source_id,
                repr(event.title),
                event.start.isoformat(),
            )

    if update_events:
        print(
            "\n=== UPDATE ==="
        )

        for event, existing in (
            update_events
        ):
            print(
                "~",
                event.source_id,
                repr(event.title),
                "google_event_id=",
                existing.google_event_id,
            )

    if unchanged_events:
        print(
            "\n=== UNCHANGED ==="
        )

        for event in unchanged_events:
            print(
                "=",
                event.source_id,
                repr(event.title),
            )

    if missing_events:
        print(
            "\n=== MISSING FROM TIMETREE ==="
        )

        if not fetch_result.complete:
            print(
                "! MISSING ACTIONS SKIPPED: "
                "TimeTree fetch incomplete"
            )

        for event in missing_events:
            new_missing_count = (
                event.missing_count + 1
            )

            if not fetch_result.complete:
                action = "SKIP"
            else:
                action = (
                    "DELETE"
                    if new_missing_count
                    >= DELETE_MISSING_THRESHOLD
                    else "INCREMENT"
                )

            print(
                "-",
                event.source_id,
                repr(event.summary),
                "missing_count=",
                event.missing_count,
                "->",
                new_missing_count,
                action,
            )

    print(
        "\n=============================="
    )

    print(
        "DRY RUN COMPLETE"
    )

    print(
        "NO GOOGLE CALENDAR "
        "CHANGES WERE MADE."
    )

    print(
        "=============================="
    )


if __name__ == "__main__":
    asyncio.run(main())
