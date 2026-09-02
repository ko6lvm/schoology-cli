#!/usr/bin/env python3
"""
Schoology Grade Fetcher
Fetches and parses Schoology grades into Python data structures (dicts/lists).
Can be imported directly into main.py or run standalone to output JSON.
"""

import os
import sys
import json
import time
import argparse
from typing import Dict, List, Optional, Any
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

DEFAULT_GRADES_URL = "https://fuhsd.schoology.com/grades/grades"
DEFAULT_PROFILE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".browser_profile"))


def parse_grades_html(html_content: str) -> List[Dict[str, Any]]:
    """
    Parses Schoology's hierarchical grading report HTML into structured Python dicts.

    Returns:
        List[Dict[str, Any]]: A list of course objects.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    course_items = soup.select("li.s-grades-course-item")
    courses = []

    for c_item in course_items:
        course_box = c_item.select_one("div.gradebook-course")
        if not course_box:
            continue

        # Extract Course ID & Title
        course_id = course_box.get("id", "").replace("s-js-gradebook-course-", "")
        title_el = course_box.select_one(".gradebook-course-title a")
        course_title = " ".join(title_el.get_text().split()) if title_el else "Unknown Course"
        if course_title.endswith("Course"):
            course_title = course_title[:-6].strip()

        # Overall Course Grade
        course_grade = ""
        c_grade_row = course_box.select_one("tr.course-row")
        if c_grade_row:
            g_col = c_grade_row.select_one("td.grade-column")
            if g_col:
                course_grade = " ".join(g_col.get_text().split())

        course_data: Dict[str, Any] = {
            "id": course_id,
            "title": course_title,
            "grade": course_grade if course_grade and course_grade != "—" else None,
            "periods": [],
        }

        table = course_box.select_one("table")
        if not table:
            courses.append(course_data)
            continue

        current_period: Optional[Dict[str, Any]] = None
        current_category: Optional[Dict[str, Any]] = None

        for r in table.select("tr.report-row"):
            classes = r.get("class", [])
            data_id = r.get("data-id", "")

            title_td = r.select_one("th.title-column")
            grade_td = r.select_one("td.grade-column")
            comment_td = r.select_one("td.comment-column")

            if not title_td:
                continue

            # Extract assignment URL if present
            link = None
            link_el = title_td.select_one("a")
            if link_el and link_el.get("href"):
                link = link_el["href"]

            # Extract due date
            due_date = None
            due_el = title_td.select_one(".due-date")
            if due_el:
                due_date = " ".join(due_el.get_text().replace("Due", "").split())
                due_el.decompose()

            # Extract percentage contribution
            contrib = None
            contrib_el = title_td.select_one(".percentage-contrib")
            if contrib_el:
                contrib = " ".join(contrib_el.get_text().strip("()").split())
                contrib_el.decompose()

            # Strip visually-hidden tags
            for vh in title_td.select(".visually-hidden"):
                vh.decompose()
            row_title = " ".join(title_td.get_text().split())

            # Extract grade
            grade_text = None
            if grade_td:
                for vh in grade_td.select(".visually-hidden"):
                    vh.decompose()
                g_val = " ".join(grade_td.get_text().split())
                if g_val and g_val != "—":
                    grade_text = g_val

            # Extract comments
            comment_text = None
            if comment_td:
                for vh in comment_td.select(".visually-hidden"):
                    vh.decompose()
                c_val = " ".join(comment_td.get_text().split())
                if c_val and c_val != "No comment":
                    comment_text = c_val

            # Hierarchy matching
            if "period-row" in classes:
                current_period = {
                    "id": data_id,
                    "title": row_title,
                    "weight": contrib,
                    "grade": grade_text,
                    "categories": [],
                }
                course_data["periods"].append(current_period)
                current_category = None

            elif "category-row" in classes:
                current_category = {
                    "id": data_id,
                    "title": row_title,
                    "weight": contrib,
                    "grade": grade_text,
                    "items": [],
                }
                if current_period:
                    current_period["categories"].append(current_category)
                else:
                    if not course_data["periods"]:
                        current_period = {
                            "id": "0",
                            "title": "General",
                            "weight": None,
                            "grade": None,
                            "categories": [],
                        }
                        course_data["periods"].append(current_period)
                    current_period["categories"].append(current_category)

            elif "item-row" in classes:
                item_data = {
                    "id": data_id,
                    "title": row_title,
                    "grade": grade_text,
                    "due_date": due_date,
                    "comment": comment_text,
                    "url": link,
                }
                if current_category:
                    current_category["items"].append(item_data)
                elif current_period:
                    if not current_period["categories"]:
                        current_category = {
                            "id": "0",
                            "title": "Uncategorized",
                            "weight": None,
                            "grade": None,
                            "items": [],
                        }
                        current_period["categories"].append(current_category)
                    current_category["items"].append(item_data)

        courses.append(course_data)

    return courses


def _handle_sso_and_auth(page, timeout_seconds: int = 120, print_logs: bool = True):
    """
    Handles Google SSO Account Chooser or prompts user to complete login.
    """
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        current_url = page.url

        # Check if already reached gradebook
        if "schoology.com" in current_url and page.locator("ul.s-grades-course-list").is_visible():
            return

        # Handle Google Account Chooser
        if "accounts.google.com" in current_url:
            if print_logs:
                print("Detecting Google SSO Account Chooser...", file=sys.stderr, flush=True)  # STATUSMSG

            selectors = [
                "div[data-email]",
                "div[data-profileidentifier]",
                "div:has-text('student.fuhsd.org')",
                "div:has-text('fuhsd.org')",
                "li:has-text('@')",
                "[data-identifier]",
            ]
            for sel in selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=1000):
                        account_label = btn.inner_text().splitlines()[0]
                        if print_logs:
                            print(f"Auto-selecting Google account: {account_label}...", file=sys.stderr, flush=True)  # STATUSMSG
                        btn.click()
                        break
                except Exception:
                    continue

        try:
            page.wait_for_selector("ul.s-grades-course-list", timeout=3000)
            if page.locator("ul.s-grades-course-list").is_visible():
                return
        except PlaywrightTimeoutError:
            pass

        time.sleep(1)

    raise TimeoutError("Timed out waiting for login / grades page load.")


def fetch_grades(
    url: str = DEFAULT_GRADES_URL,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    headless: bool = True,
    timeout_seconds: int = 120,
    file_path: Optional[str] = None,
    save_html_path: Optional[str] = None,
    print_logs: bool = True,
    show_status: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """
    Main function to fetch Schoology grades as Python data structures.

    Args:
        url: Schoology grades URL.
        profile_dir: Path to persistent browser profile directory.
        headless: If True, runs browser in background; if False, opens visible window.
        timeout_seconds: Timeout for login / page load.
        file_path: If provided, parses local HTML file instead of opening browser.
        save_html_path: If provided, saves fetched HTML to this file path.
        print_logs: If True, prints live status logs to stderr.
        show_status: Alias for print_logs (for backwards compatibility).

    Returns:
        List[Dict[str, Any]]: Structured Python dictionary of all courses, periods, categories, and assignments.
    """
    if show_status is not None:
        print_logs = show_status

    # 1. Parse from local file if specified
    if file_path:
        if print_logs:
            print(f"Reading grades from local file: {file_path}...", file=sys.stderr, flush=True)  # STATUSMSG
        with open(file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        if print_logs:
            print("Parsing gradebook hierarchy...", file=sys.stderr, flush=True)  # STATUSMSG
        courses = parse_grades_html(html_content)
        if print_logs:
            print(f"Successfully parsed {len(courses)} courses.", file=sys.stderr, flush=True)  # STATUSMSG
        return courses

    # 2. Fetch live via persistent browser session
    os.makedirs(profile_dir, exist_ok=True)
    if print_logs:
        mode_text = "headless background" if headless else "visible window"
        print(f"Starting browser session ({mode_text})...", file=sys.stderr, flush=True)  # STATUSMSG

    with sync_playwright() as p:
        if print_logs:
            print("Loading browser profile and cookies...", file=sys.stderr, flush=True)  # STATUSMSG

        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = context.pages[0] if context.pages else context.new_page()

        if print_logs:
            print(f"Navigating to {url}...", file=sys.stderr, flush=True)  # STATUSMSG
        page.goto(url, wait_until="domcontentloaded")

        try:
            if print_logs:
                print("Authenticating and waiting for gradebook page to load...", file=sys.stderr, flush=True)  # STATUSMSG
            _handle_sso_and_auth(page, timeout_seconds=timeout_seconds, print_logs=print_logs)

            if print_logs:
                print("Gradebook loaded. Rendering page content...", file=sys.stderr, flush=True)  # STATUSMSG
            page.wait_for_load_state("networkidle")
            html_content = page.content()

            if print_logs:
                print("HTML content extracted successfully.", file=sys.stderr, flush=True)  # STATUSMSG
        finally:
            context.close()

    if save_html_path:
        if print_logs:
            print(f"Saving raw HTML to {save_html_path}...", file=sys.stderr, flush=True)  # STATUSMSG
        with open(save_html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

    if print_logs:
        print("Parsing course grades and assignment data...", file=sys.stderr, flush=True)  # STATUSMSG
    courses = parse_grades_html(html_content)

    if print_logs:
        print(f"Done! Extracted {len(courses)} courses.", file=sys.stderr, flush=True)  # STATUSMSG

    return courses


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Schoology grades and return structured JSON."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser in headless mode (default: True)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_false",
        dest="headless",
        help="Open visible browser window (for first-time manual login)",
    )
    parser.add_argument(
        "--print-logs",
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Toggle printing status logs to stderr (default: True, use --no-logs or --no-print-logs to disable)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_false",
        dest="print_logs",
        help="Suppress status update messages on stderr (alias for --no-logs)",
    )
    parser.add_argument(
        "--file",
        "-f",
        help="Parse a local HTML file instead of opening browser",
    )
    parser.add_argument(
        "--save-html",
        help="Save the fetched HTML to a local file",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_GRADES_URL,
        help=f"Schoology grades URL (default: {DEFAULT_GRADES_URL})",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE_DIR,
        help="Browser profile directory",
    )

    args = parser.parse_args()

    try:
        grades = fetch_grades(
            url=args.url,
            profile_dir=args.profile,
            headless=args.headless,
            file_path=args.file,
            save_html_path=args.save_html,
            print_logs=args.print_logs,
        )
        # Output clean JSON to stdout
        print(json.dumps(grades, indent=2))
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
