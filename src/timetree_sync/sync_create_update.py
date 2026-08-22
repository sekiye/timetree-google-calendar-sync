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
        "=== TIMETREE -> GOOGLE CALENDAR ==="
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

    if not source_events:
        raise RuntimeError(
            "No TimeTree events found. "
            "Aborting sync to prevent unsafe missing/delete processing."
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

    source_ids = {
        event.source_id
        for event in source_events
    }

    matching_source_ids = (
        source_ids
        & set(google_events.keys())
    )

    print(
        "SOURCE ID MATCHES:",
        len(matching_source_ids),
    )

    # JSON方式への切替時の重複作成防止。
    # 既存managed eventがあるのに一致が1件もない場合、
    # source_id互換性に問題がある可能性が高いので書込み前に停止する。
    if (
        google_events
        and source_events
        and not matching_source_ids
    ):
        raise RuntimeError(
            "No source_id matched between TimeTree and "
            "existing Google events. Aborting before writes "
            "to prevent duplicate creation. Run dry_run.py "
            "and inspect source IDs."
        )

    created_count = 0
    updated_count = 0
    unchanged_count = 0
    reset_missing_count = 0
    missing_incremented_count = 0
    deleted_count = 0

    # ==========================================
    # CREATE / UPDATE / PRESENT
    # ==========================================

    for event in source_events:
        existing = google_events.get(
            event.source_id
        )

        expected_hash = (
            source_event_hash(event)
        )

        # 新規
        if existing is None:
            print(
                "\nCREATE:",
                event.source_id,
            )

            google.create_event(
                event
            )

            created_count += 1

            continue

        # 内容変更あり
        if (
            existing.source_hash
            != expected_hash
        ):
            print(
                "\nUPDATE:",
                event.source_id,
            )

            google.update_event(
                google_event_id=(
                    existing.google_event_id
                ),
                event=event,
            )

            updated_count += 1

            continue

        # 内容は同じだが、
        # 以前missingだった予定が復活した
        if existing.missing_count != 0:
            print(
                "\nRESET MISSING:",
                event.source_id,
                existing.missing_count,
                "-> 0",
            )

            google.set_missing_count(
                google_event_id=(
                    existing.google_event_id
                ),
                missing_count=0,
            )

            reset_missing_count += 1

            continue

        unchanged_count += 1

    # ==========================================
    # MISSING / DELETE
    # ==========================================

    if fetch_result.complete:
        for (
            source_id,
            existing,
        ) in google_events.items():
            if source_id in source_ids:
                continue

            new_missing_count = (
                existing.missing_count
                + 1
            )

            print(
                "\nMISSING:",
                source_id,
                existing.missing_count,
                "->",
                new_missing_count,
            )

            if (
                new_missing_count
                >= DELETE_MISSING_THRESHOLD
            ):
                print(
                    "  DELETE:",
                    existing.google_event_id,
                )

                google.delete_event(
                    google_event_id=(
                        existing.google_event_id
                    ),
                )

                deleted_count += 1

                continue

            google.set_missing_count(
                google_event_id=(
                    existing.google_event_id
                ),
                missing_count=(
                    new_missing_count
                ),
            )

            missing_incremented_count += 1
    else:
        print(
            "\nMISSING/DELETE: SKIPPED "
            "because TimeTree fetch was incomplete."
        )

    # ==========================================
    # Result
    # ==========================================

    print(
        "\n=============================="
    )

    print("SYNC RESULT")

    print(
        "=============================="
    )

    print(
        "CREATED:",
        created_count,
    )

    print(
        "UPDATED:",
        updated_count,
    )

    print(
        "UNCHANGED:",
        unchanged_count,
    )

    print(
        "MISSING RESET:",
        reset_missing_count,
    )

    print(
        "MISSING INCREMENTED:",
        missing_incremented_count,
    )

    print(
        "DELETED:",
        deleted_count,
    )

    if fetch_result.complete:
        print(
            "\nDELETE: ENABLED "
            f"(threshold={DELETE_MISSING_THRESHOLD})"
        )
    else:
        print(
            "\nDELETE: DISABLED FOR THIS RUN "
            "(incomplete TimeTree fetch)"
        )

    print(
        "=============================="
    )


if __name__ == "__main__":
    asyncio.run(main())
