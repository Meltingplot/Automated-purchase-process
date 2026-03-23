"""
Frappe integration smoke tests.

Validates that the app is correctly installed: DocTypes exist, custom fields
are applied, hooks are loaded, and the scheduler function is resolvable.

Run via: bench execute procurement_ai.tests.test_frappe_integration.run_all
"""

from __future__ import annotations


def run_all():
    """Run all integration checks."""
    failures = []

    checks = [
        ("DocTypes exist", check_doctypes_exist),
        ("Custom fields installed", check_custom_fields),
        ("Hooks loaded", check_hooks_loaded),
        ("Settings singleton accessible", check_settings_singleton),
        ("Scheduler function resolvable", check_scheduler_function),
        ("API endpoints resolvable", check_api_endpoints),
        ("Fixtures loaded", check_fixtures_loaded),
    ]

    for name, fn in checks:
        try:
            fn()
            print(f"  OK  {name}")
        except Exception as exc:
            failures.append((name, str(exc)))
            print(f"  FAIL  {name}: {exc}")

    print()
    print(f"Passed {len(checks) - len(failures)}/{len(checks)} integration checks.")

    if failures:
        print("\nFailed checks:")
        for name, err in failures:
            print(f"  - {name}: {err}")
        raise SystemExit(1)

    print("All integration checks passed.")


def check_doctypes_exist():
    """Verify all 4 DocTypes are registered."""
    import frappe

    expected = [
        "AI Procurement Job",
        "AI Extraction Result",
        "AI Procurement Settings",
        "AI Escalation Log",
    ]
    for dt in expected:
        assert frappe.db.exists("DocType", dt), f"DocType '{dt}' not found"


def check_custom_fields():
    """Verify custom fields on PO/PR/PI were created by fixtures."""
    import frappe

    for parent_dt in ("Purchase Order", "Purchase Receipt", "Purchase Invoice"):
        field_name = f"{parent_dt}-ai_procurement_job"
        exists = frappe.db.exists("Custom Field", field_name)
        assert exists, (
            f"Custom Field '{field_name}' not found. "
            "Did bench migrate apply the fixtures?"
        )


def check_hooks_loaded():
    """Verify the app appears in installed apps and hooks are accessible."""
    import frappe

    installed = frappe.get_installed_apps()
    assert "procurement_ai" in installed, (
        f"procurement_ai not in installed apps: {installed}"
    )

    hooks = frappe.get_hooks("scheduler_events")
    all_events = hooks.get("all", [])
    expected = "procurement_ai.procurement_ai.api.ingest.process_pending_jobs"
    assert expected in all_events, (
        f"Scheduler hook not found. 'all' events: {all_events}"
    )


def check_settings_singleton():
    """Verify the Settings singleton DocType can be read."""
    import frappe

    doc = frappe.get_single("AI Procurement Settings")
    assert doc is not None
    assert doc.doctype == "AI Procurement Settings"


def check_scheduler_function():
    """Verify the scheduler target function can be resolved."""
    fn = frappe.get_attr(
        "procurement_ai.procurement_ai.api.ingest.process_pending_jobs"
    )
    assert callable(fn), "process_pending_jobs is not callable"


def check_api_endpoints():
    """Verify API module functions are importable and whitelisted."""
    from procurement_ai.procurement_ai.api.ingest import process as ingest_process
    from procurement_ai.procurement_ai.api.status import (
        get_dashboard_stats,
        get_job_status,
    )

    for fn in (ingest_process, get_job_status, get_dashboard_stats):
        assert callable(fn), f"{fn} is not callable"


def check_fixtures_loaded():
    """Verify fixture Custom Fields have correct properties."""
    import frappe

    for parent_dt in ("Purchase Order", "Purchase Receipt", "Purchase Invoice"):
        field_name = f"{parent_dt}-ai_procurement_job"
        if not frappe.db.exists("Custom Field", field_name):
            continue
        cf = frappe.get_doc("Custom Field", field_name)
        assert cf.fieldtype == "Link", f"{field_name} should be Link, got {cf.fieldtype}"
        assert cf.options == "AI Procurement Job", (
            f"{field_name} should link to AI Procurement Job, got {cf.options}"
        )
        assert cf.read_only == 1, f"{field_name} should be read_only"
