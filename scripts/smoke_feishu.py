"""
飞书一期冒烟：健康检查 →（可选）写 1 条日程 →（可选）写 1 行计划表。

用法（在 cyber_lobster_api_fusion 目录）：
  ..\venv311\Scripts\python.exe scripts\smoke_feishu.py              # 只检查，不写
  ..\venv311\Scripts\python.exe scripts\smoke_feishu.py --write      # 检查 + 日历 + 计划表
  ..\venv311\Scripts\python.exe scripts\smoke_feishu.py --write --calendar-only
  ..\venv311\Scripts\python.exe scripts\smoke_feishu.py --write --plan-only

退出码：0 通过；1 失败；2 配置不齐（未 --write 时仅告警也可 0）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]  # cyber_lobster_api_fusion
_REPO = Path(__file__).resolve().parents[2]  # agent项目（.env）
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

try:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
except Exception:
    pass

from apitools.feishu import (  # noqa: E402
    add_plan_rows,
    create_calendar_event,
    feishu_auth_health,
    fetch_calendar_events,
    fetch_plan_table,
)

_TZ = timezone(timedelta(hours=8))


def _print(title: str, obj) -> None:
    print(f"\n=== {title} ===")
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj)


def main() -> int:
    ap = argparse.ArgumentParser(description="Feishu Phase-1 smoke test")
    ap.add_argument(
        "--write",
        action="store_true",
        help="actually create one calendar event and/or one plan row",
    )
    ap.add_argument("--calendar-only", action="store_true")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    health = feishu_auth_health()
    _print("feishu_auth_health", health)
    if health.get("issues"):
        print("\nIssues:")
        for i in health["issues"]:
            print(f"  - {i}")

    if not health.get("app_configured"):
        print("\nFAIL: Feishu app not configured.")
        return 2

    if not args.write:
        if health.get("ok"):
            print("\nOK: health check passed (read-only). Use --write to exercise APIs.")
            return 0
        print("\nWARN: health not fully ready. Fix issues above, or pass --write to probe anyway.")
        return 0 if not health.get("user_token_cached") else 1

    do_cal = not args.plan_only
    do_plan = not args.calendar_only
    stamp = datetime.now(tz=_TZ).strftime("%Y%m%d_%H%M%S")
    failures: list[str] = []

    if do_cal:
        if not health.get("write_calendar_ready"):
            failures.append("calendar: write path not ready (oauth/scope)")
        else:
            # 放到约 30 天后中午，避免污染近日视图
            start = (datetime.now(tz=_TZ) + timedelta(days=30)).replace(
                hour=12, minute=0, second=0, microsecond=0
            )
            end = start + timedelta(hours=1)
            try:
                created = create_calendar_event(
                    summary=f"【冒烟】smoke_{stamp}",
                    start_at=start.strftime("%Y-%m-%d %H:%M"),
                    end_at=end.strftime("%Y-%m-%d %H:%M"),
                    description="cyber_lobster smoke_feishu.py — safe to delete",
                )
                _print("create_calendar_event", created)
                listed = fetch_calendar_events(days=1)
                _print(
                    "fetch_calendar_events(sample)",
                    {
                        "event_count": listed.get("event_count"),
                        "auth_mode": listed.get("auth_mode"),
                        "calendar_id": listed.get("calendar_id"),
                    },
                )
            except Exception as e:
                failures.append(f"calendar: {e}")
                print(f"\ncalendar FAILED: {e}")

    if do_plan:
        if health.get("user_token_cached") and not health.get("bitable_ready"):
            failures.append("plan: bitable scope missing")
        else:
            try:
                rows = add_plan_rows(
                    [
                        {
                            "日期": datetime.now(tz=_TZ).strftime("%Y-%m-%d"),
                            "事项": f"【冒烟】smoke_{stamp}",
                            "优先级": "低",
                            "来源": "smoke",
                            "状态": "待办",
                            "备注": "smoke_feishu.py — safe to delete",
                        }
                    ]
                )
                _print("add_plan_rows", rows)
                table = fetch_plan_table()
                _print(
                    "fetch_plan_table(sample)",
                    {
                        "record_count": table.get("record_count")
                        or len(table.get("records") or []),
                        "app_token": table.get("app_token"),
                        "table_id": table.get("table_id"),
                    },
                )
            except Exception as e:
                failures.append(f"plan: {e}")
                print(f"\nplan FAILED: {e}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nOK: smoke write path passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
