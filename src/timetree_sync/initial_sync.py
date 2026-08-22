import asyncio
import os

from dotenv import load_dotenv

from timetree_sync.google_calendar import (
    GoogleCalendarClient,
)
from timetree_sync.timetree import (
    fetch_timetree_events,
)


async def main() -> None:
    load_dotenv()

    print(
        "=== INITIAL TIMETREE -> "
        "GOOGLE CALENDAR SYNC ==="
    )

    # ==========================================
    # Fetch TimeTree
    # ==========================================

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

    if not fetch_result.complete:
        raise RuntimeError(
            "TimeTree fetch was incomplete. "
            "Initial sync aborted."
        )

    if not source_events:
        raise RuntimeError(
            "No TimeTree events found. "
            "Aborting initial sync."
        )

    # ==========================================
    # Google Calendar
    # ==========================================

    print(
        "\n2. Checking Google Calendar..."
    )

    google = GoogleCalendarClient(
        credentials_path=os.environ[
            "GOOGLE_SERVICE_ACCOUNT_JSON"
        ],
        calendar_id=os.environ[
            "GOOGLE_CALENDAR_ID"
        ],
    )

    existing = (
        google.list_managed_events()
    )

    print(
        "GOOGLE MANAGED EVENTS:",
        len(existing),
    )

    # 初回同期専用。
    # 既存同期イベントがあれば二重作成防止のため停止。
    if existing:
        raise RuntimeError(
            "Managed Google events already exist. "
            "Initial sync aborted to prevent "
            "duplicate creation."
        )

    # ==========================================
    # Create
    # ==========================================

    print(
        "\n3. Creating Google events..."
    )

    created_count = 0

    for index, event in enumerate(
        source_events,
        start=1,
    ):
        print(
            f"CREATE {index}/"
            f"{len(source_events)}:"
        )

        print(
            " ",
            event.source_id,
            repr(event.title),
            event.start.isoformat(),
        )

        created = google.create_event(
            event
        )

        created_count += 1

        print(
            "  OK:",
            created.google_event_id,
        )

    print(
        "\n=============================="
    )

    print(
        "INITIAL SYNC COMPLETE"
    )

    print(
        "CREATED:",
        created_count,
    )

    print(
        "=============================="
    )

    # ==========================================
    # Verify
    # ==========================================

    print(
        "\n4. Verifying Google Calendar..."
    )

    managed = (
        google.list_managed_events()
    )

    print(
        "GOOGLE MANAGED EVENTS:",
        len(managed),
    )

    expected_ids = {
        event.source_id
        for event in source_events
    }

    actual_ids = set(
        managed.keys()
    )

    if expected_ids != actual_ids:
        missing = (
            expected_ids
            - actual_ids
        )

        unexpected = (
            actual_ids
            - expected_ids
        )

        raise RuntimeError(
            "Verification failed. "
            f"missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )

    print(
        "SOURCE ID VERIFICATION: OK"
    )

    print(
        "\nINITIAL SYNC: OK"
    )


if __name__ == "__main__":
    asyncio.run(main())
