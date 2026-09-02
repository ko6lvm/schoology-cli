#!/usr/bin/env python3
"""
Schoology Assignment Fetcher
Fetches assignments and assignment details for a given course from Schoology.
Can be imported as a module or run directly from the command line.
"""

import os
import sys
import json
import time
import re
import argparse
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Any, Union
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

DEFAULT_BASE_URL = "https://fuhsd.schoology.com"
DEFAULT_PROFILE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".browser_profile"))


# ---------------------------------------------------------------------------
# HTML Parsers (Pure functions for parsing and unit testing)
# ---------------------------------------------------------------------------

def parse_courses_html(html_content: str, base_url: str = DEFAULT_BASE_URL) -> List[Dict[str, Any]]:
    """
    Parses Schoology's /courses page HTML into a list of course dictionaries.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    courses: List[Dict[str, Any]] = []

    course_items = soup.select(".courses-course-item, .course-item, .my-courses-item")
    for item in course_items:
        title_el = item.select_one(".course-title, .title, h3, h4")
        section_el = item.select_one(".section-title, .section-name, .course-section, a[href*='/course/']")
        link_el = item.select_one("a[href*='/course/']")

        href = link_el["href"] if link_el and link_el.get("href") else None
        cid_match = re.search(r"/course/(\d+)", href) if href else None
        cid = cid_match.group(1) if cid_match else None

        title = " ".join(title_el.get_text().split()) if title_el else ""
        section = " ".join(section_el.get_text().split()) if section_el else ""
        full_url = urljoin(base_url, href) if href else None

        if cid or title:
            courses.append({
                "id": cid,
                "title": title,
                "section": section,
                "url": full_url,
            })

    # Fallback to course dropdown / gradebook course links if /courses layout differed
    if not courses:
        for a in soup.select("a[href*='/course/']"):
            href = a.get("href", "")
            cid_match = re.search(r"/course/(\d+)", href)
            if cid_match:
                cid = cid_match.group(1)
                text = " ".join(a.get_text().split())
                if text and not any(c["id"] == cid for c in courses):
                    courses.append({
                        "id": cid,
                        "title": text,
                        "section": "",
                        "url": urljoin(base_url, href),
                    })

    return courses


def parse_materials_assignments_html(html_content: str, base_url: str = DEFAULT_BASE_URL) -> List[Dict[str, Any]]:
    """
    Parses the course materials page filtered by assignments
    (/course/{course_id}/materials?list_filter=assignments).
    """
    soup = BeautifulSoup(html_content, "html.parser")
    assignments: List[Dict[str, Any]] = []

    rows = soup.select(".filtered-view-list-row")
    for row in rows:
        title_a = row.select_one(".s-common-block_title a")
        if not title_a:
            continue

        raw_title = " ".join(title_a.get_text().split())
        title = re.sub(r"\s*·?\s*\d*\s*lesson plans?", "", raw_title).strip()
        href = title_a.get("href", "")
        full_url = urljoin(base_url, href) if href else None

        aid_match = re.search(r"/assignment/(\d+)", href)
        aid = aid_match.group(1) if aid_match else None

        # Parent Folder
        parent_folder = None
        folder_tip = row.select_one(".materials-filtered-parent-folder .infotip-content, .materials-filtered-parent-folder span[role='tooltip']")
        if folder_tip:
            folder_text = " ".join(folder_tip.get_text().split())
            parent_folder = re.sub(r"^Parent folder:\s*", "", folder_text).strip()

        # Category from subtitle
        category = None
        subtitle_el = row.select_one(".item-subtitle")
        if subtitle_el:
            sub_copy = BeautifulSoup(str(subtitle_el), "html.parser")
            for extra in sub_copy.select(".lesson-plan-wrapper, .lesson-plan-text, span[id*='lesson-plan']"):
                extra.decompose()
            category_text = " ".join(sub_copy.get_text().split()).strip(" ·")
            if category_text:
                category = category_text

        # Description / instruction snippet in listing
        snippet = None
        content_el = row.select_one(".s-common-block_content")
        if content_el:
            snippet_texts = []
            for child in content_el.find_all(recursive=False):
                cls = child.get("class", [])
                if "s-common-block_title" in cls or "s-common-block_copy" in cls:
                    continue
                txt = " ".join(child.get_text().split())
                if txt:
                    snippet_texts.append(txt)
            if snippet_texts:
                snippet = "\n".join(snippet_texts)

        assignments.append({
            "id": aid,
            "title": title,
            "url": full_url,
            "category": category,
            "folder": parent_folder,
            "description_snippet": snippet,
        })

    return assignments


def parse_assignment_detail_html(html_content: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    """
    Parses a single assignment's detail page (/assignment/{id}/info).
    Extracts due date, posted date, full description, category, grading period,
    attachments, submissions, and comments count.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # Assignment ID
    aid = None
    canonical = soup.select_one("link[rel='canonical']")
    if canonical and canonical.get("href"):
        m = re.search(r"/assignment/(\d+)", canonical["href"])
        if m:
            aid = m.group(1)
    if not aid:
        form = soup.select_one("form[action*='/assignment/']")
        if form and form.get("action"):
            m = re.search(r"/assignment/(\d+)", form["action"])
            if m:
                aid = m.group(1)

    # Title
    title = None
    title_el = soup.select_one("h1.page-title, h2.page-title, .page-title")
    if title_el:
        t_copy = BeautifulSoup(str(title_el), "html.parser")
        for bad in t_copy.select("[aria-label*='lesson plan'], .lesson-plan-wrapper, .lesson-plan-text, span[id*='lesson-plan']"):
            bad.decompose()
        clean = " ".join(t_copy.get_text().split())
        title = re.sub(r"\s*\d*\s*lesson plans?", "", clean).strip()

    # Due Date
    due_date = None
    for due_el in soup.select(".due-date, p.due-date, .assignment-details .due-date"):
        text = " ".join(due_el.get_text().split())
        if text:
            due_date = re.sub(r"^Due:\s*", "", text, flags=re.IGNORECASE).strip()
            break

    # Posted Date
    posted_date = None
    posted_el = soup.select_one(".posted-time, p.posted-time")
    if posted_el:
        text = " ".join(posted_el.get_text().split())
        posted_date = re.sub(r"^Posted\s*", "", text, flags=re.IGNORECASE).strip()

    # Description (Text and HTML)
    description = None
    description_html = None
    desc_el = soup.select_one(".info-body, .assignment-description")
    if desc_el:
        description = desc_el.get_text(separator="\n", strip=True)
        description_html = str(desc_el)

    # Grading Category & Period from .grading-info
    category = None
    grading_period = None

    cat_el = soup.select_one(".grading-category")
    if cat_el:
        c_copy = BeautifulSoup(str(cat_el), "html.parser")
        for p in c_copy.select(".param-name"):
            p.decompose()
        category = " ".join(c_copy.get_text().split())

    per_el = soup.select_one(".grading-period")
    if per_el:
        p_copy = BeautifulSoup(str(per_el), "html.parser")
        for p in p_copy.select(".param-name"):
            p.decompose()
        grading_period = " ".join(p_copy.get_text().split())

    # Fallback to sidebar definition list (<dl>)
    if not category or not grading_period:
        for dl in soup.select("dl"):
            dt = dl.select_one("dt")
            dd = dl.select_one("dd")
            if dt and dd:
                lbl = dt.get_text(strip=True).lower()
                val = " ".join(dd.get_text().split())
                if "category" in lbl and not category:
                    category = val
                elif "grading period" in lbl and not grading_period:
                    grading_period = val

    # Attachments
    attachments = []
    for att in soup.select(".attachments-file, .attachment-item"):
        att_copy = BeautifulSoup(str(att), "html.parser")
        # remove tooltips, hidden elements, and action buttons from name
        for bad in att_copy.select(".infotip-content, .visually-hidden, .attachments-file-size, .view-file-popup, a.view-file-popup"):
            bad.decompose()

        link_el = att_copy.select_one("a[href]")
        orig_link = att.select_one("a[href]")
        size_el = att.select_one(".attachments-file-size, .attachment-size, .filesize, .size")
        viewer_el = att.select_one("a.view-file-popup, a[href*='docviewer']")

        name = " ".join(link_el.get_text().split()) if link_el else " ".join(att_copy.get_text().split())
        name = re.sub(r"\s*(VIEW|DOWNLOAD)$", "", name).strip()
        url = urljoin(base_url, orig_link["href"]) if orig_link and orig_link.get("href") else None
        viewer_url = urljoin(base_url, viewer_el["href"]) if viewer_el and viewer_el.get("href") else None
        size = " ".join(size_el.get_text().split()) if size_el else None

        attachments.append({
            "title": name,
            "url": url,
            "viewer_url": viewer_url,
            "size": size,
        })

    # Submissions / Dropbox
    submissions = []
    sub_box = soup.select_one(".dropbox-revisions, #dropbox-revisions")
    if sub_box:
        for li in sub_box.select("li"):
            rev_link = li.select_one("a.dropbox-view-link, a[href*='dropbox']")
            status_el = li.select_one(".submission-status")
            desc_sub = li.select_one(".description")
            rev_date = li.get("original-title") or (" ".join(rev_link.get_text().split()) if rev_link else "")
            rev_url = urljoin(base_url, rev_link["href"]) if rev_link and rev_link.get("href") else None

            submissions.append({
                "submitted_at": rev_date,
                "status": " ".join(status_el.get_text().split()) if status_el else None,
                "summary": " ".join(desc_sub.get_text().split()) if desc_sub else None,
                "url": rev_url,
            })

    # Comments count
    comments_count = 0
    comments_label = soup.select_one(".comment-container-header-label")
    if not comments_label:
        for h in soup.find_all("h3"):
            if "comment" in h.get_text().lower():
                comments_label = h
                break
    if comments_label:
        c_text = comments_label.get_text(strip=True)
        m = re.search(r"\((\d+)\)", c_text)
        if m:
            comments_count = int(m.group(1))
        else:
            comments_count = len(soup.select(".comment-contents-container .comment-item, .s-comment"))

    return {
        "id": aid,
        "title": title,
        "due_date": due_date,
        "posted_date": posted_date,
        "category": category,
        "grading_period": grading_period,
        "description": description,
        "description_html": description_html,
        "attachments": attachments,
        "submissions": submissions,
        "comments_count": comments_count,
    }


# ---------------------------------------------------------------------------
# Browser Session & Auth Helpers
# ---------------------------------------------------------------------------

def _handle_sso_and_auth(page, timeout_seconds: int = 120, print_logs: bool = True):
    """
    Handles Google SSO Account Chooser or prompts user to complete login.
    """
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        current_url = page.url

        # Check if already reached a logged in Schoology page
        if "schoology.com" in current_url and "login" not in current_url:
            return

        # Handle Google Account Chooser
        if "accounts.google.com" in current_url:
            if print_logs:
                print("Detecting Google SSO Account Chooser...", file=sys.stderr, flush=True)

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
                            print(f"Auto-selecting Google account: {account_label}...", file=sys.stderr, flush=True)
                        btn.click()
                        break
                except Exception:
                    continue

        time.sleep(1)

    raise TimeoutError("Timed out waiting for login to complete.")


def get_browser_session(
    base_url: str = DEFAULT_BASE_URL,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    headless: bool = True,
    timeout_seconds: int = 120,
    print_logs: bool = True,
):
    """
    Context manager that launches Playwright with a persistent browser profile,
    navigates to the Schoology base URL, handles SSO auth, and yields (context, page).
    """
    class SessionManager:
        def __init__(self):
            self.playwright = None
            self.context = None
            self.page = None

        def __enter__(self):
            os.makedirs(profile_dir, exist_ok=True)
            if print_logs:
                mode_text = "headless background" if headless else "visible window"
                print(f"Starting browser session ({mode_text})...", file=sys.stderr, flush=True)

            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

            if print_logs:
                print(f"Navigating to {base_url}...", file=sys.stderr, flush=True)
            self.page.goto(base_url, wait_until="domcontentloaded")
            _handle_sso_and_auth(self.page, timeout_seconds=timeout_seconds, print_logs=print_logs)
            self.page.wait_for_load_state("networkidle")
            if print_logs:
                print("Session established successfully.", file=sys.stderr, flush=True)
            return self.context, self.page

        def __exit__(self, exc_type, exc_val, exc_tb):
            if self.context:
                self.context.close()
            if self.playwright:
                self.playwright.stop()

    return SessionManager()


def create_authenticated_http_session(page_or_context) -> requests.Session:
    """
    Extracts authenticated cookies from a Playwright Page or BrowserContext
    and creates a requests.Session pre-loaded with those cookies and headers.
    """
    context = page_or_context.context if hasattr(page_or_context, "context") else page_or_context
    cookies = context.cookies()

    session = requests.Session()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])

    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


# ---------------------------------------------------------------------------
# High-Level Fetching Functions
# ---------------------------------------------------------------------------

def fetch_courses(
    base_url: str = DEFAULT_BASE_URL,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    headless: bool = True,
    timeout_seconds: int = 120,
    print_logs: bool = True,
    page = None,
) -> List[Dict[str, Any]]:
    """
    Fetches all enrolled Schoology courses for the current user.
    """
    def _fetch_from_page(p) -> List[Dict[str, Any]]:
        courses_url = urljoin(base_url, "/courses")
        if print_logs:
            print(f"Fetching course list from {courses_url}...", file=sys.stderr, flush=True)
        p.goto(courses_url, wait_until="domcontentloaded")
        _handle_sso_and_auth(p, timeout_seconds=timeout_seconds, print_logs=print_logs)
        p.wait_for_load_state("networkidle")
        courses = parse_courses_html(p.content(), base_url=base_url)
        if print_logs:
            print(f"Found {len(courses)} courses.", file=sys.stderr, flush=True)
        return courses

    if page is not None:
        return _fetch_from_page(page)

    with get_browser_session(base_url=base_url, profile_dir=profile_dir, headless=headless, timeout_seconds=timeout_seconds, print_logs=print_logs) as (_, p):
        return _fetch_from_page(p)


def fetch_assignment_details(
    assignment_id_or_url: Union[str, int],
    base_url: str = DEFAULT_BASE_URL,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    headless: bool = True,
    timeout_seconds: int = 120,
    print_logs: bool = True,
    page = None,
) -> Dict[str, Any]:
    """
    Fetches detailed data for a specific assignment given its ID or URL.
    """
    def _fetch_from_page(p, target_ref) -> Dict[str, Any]:
        ref_str = str(target_ref).strip()
        if ref_str.startswith("http"):
            target_url = ref_str
        elif ref_str.startswith("/"):
            target_url = urljoin(base_url, ref_str)
        else:
            target_url = urljoin(base_url, f"/assignment/{ref_str}/info")

        if not target_url.endswith("/info") and "/dropbox" not in target_url:
            target_url = target_url.rstrip("/") + "/info"

        if print_logs:
            print(f"Fetching assignment details from {target_url}...", file=sys.stderr, flush=True)

        p.goto(target_url, wait_until="domcontentloaded")
        _handle_sso_and_auth(p, timeout_seconds=timeout_seconds, print_logs=print_logs)
        p.wait_for_load_state("networkidle")

        details = parse_assignment_detail_html(p.content(), base_url=base_url)
        details["url"] = target_url
        return details

    if page is not None:
        return _fetch_from_page(page, assignment_id_or_url)

    with get_browser_session(base_url=base_url, profile_dir=profile_dir, headless=headless, timeout_seconds=timeout_seconds, print_logs=print_logs) as (_, p):
        return _fetch_from_page(p, assignment_id_or_url)


def fetch_course_assignments(
    course: Union[str, int, Dict[str, Any]],
    fetch_details: bool = True,
    parallel: bool = True,
    max_workers: int = 2,
    base_url: str = DEFAULT_BASE_URL,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    headless: bool = True,
    timeout_seconds: int = 120,
    print_logs: bool = True,
    page = None,
) -> Dict[str, Any]:
    """
    Retrieves data on each assignment for a given course.

    Args:
        course: Can be:
            - Course ID (e.g. 8465379643 or "8465379643")
            - Course Title / Substring (e.g. "Java", "Algebra", "Biology")
            - Course URL (e.g. "https://fuhsd.schoology.com/course/8465379643/materials")
            - A course dictionary with an "id" or "url" key
        fetch_details: If True, visits each assignment's page to extract full description,
            exact due date, attachments, and submissions. If False, only extracts materials listing.
        parallel: If True, uses a multithreaded HTTP session pool to fetch assignment details concurrently.
        max_workers: Concurrency limit for parallel requests (default: 2).
        base_url: Schoology base URL.
        profile_dir: Persistent browser profile directory.
        headless: Run browser in background if True.
        timeout_seconds: Maximum wait time for network and auth.
        print_logs: If True, outputs status logs to stderr.
        page: Optional existing Playwright Page to reuse an active session.

    Returns:
        Dict[str, Any]: Course assignment details and list of assignment dictionaries.
    """
    def _fetch(p) -> Dict[str, Any]:
        course_id: Optional[str] = None
        course_title: Optional[str] = None

        if isinstance(course, dict):
            course_id = str(course.get("id")) if course.get("id") else None
            course_title = course.get("title")
            if not course_id and course.get("url"):
                m = re.search(r"/course/(\d+)", course["url"])
                if m:
                    course_id = m.group(1)
        else:
            query = str(course).strip()
            if query.isdigit():
                course_id = query
            elif "/course/" in query:
                m = re.search(r"/course/(\d+)", query)
                if m:
                    course_id = m.group(1)
            else:
                if print_logs:
                    print(f"Resolving course by name query '{query}'...", file=sys.stderr, flush=True)
                available_courses = fetch_courses(base_url=base_url, profile_dir=profile_dir, headless=headless, timeout_seconds=timeout_seconds, print_logs=print_logs, page=p)
                for c in available_courses:
                    full_name = f"{c.get('title', '')} {c.get('section', '')}".lower()
                    if query.lower() in full_name:
                        course_id = c["id"]
                        course_title = c["title"]
                        break
                if not course_id:
                    raise ValueError(f"No course matched query '{query}'. Available: {[c['title'] for c in available_courses]}")

        if not course_id:
            raise ValueError(f"Could not determine Course ID from input: {course}")

        materials_url = urljoin(base_url, f"/course/{course_id}/materials?list_filter=assignments")
        if print_logs:
            print(f"Navigating to course materials: {materials_url}...", file=sys.stderr, flush=True)

        p.goto(materials_url, wait_until="domcontentloaded")
        _handle_sso_and_auth(p, timeout_seconds=timeout_seconds, print_logs=print_logs)
        p.wait_for_load_state("networkidle")

        actual_title = p.title().split("|")[0].strip()
        if not course_title:
            course_title = actual_title

        # Parse assignments from materials list
        assignments = parse_materials_assignments_html(p.content(), base_url=base_url)
        if print_logs:
            print(f"Found {len(assignments)} assignments in course '{course_title}'.", file=sys.stderr, flush=True)

        # Detailed retrieval per assignment
        if fetch_details and assignments:
            if parallel:
                if print_logs:
                    workers_count = min(max_workers, len(assignments))
                    print(f"Fetching full details for {len(assignments)} assignments in parallel ({workers_count} workers)...", file=sys.stderr, flush=True)

                http_session = create_authenticated_http_session(p)
                completed_count = 0
                counter_lock = threading.Lock()

                def _fetch_single_assignment(asgn: Dict[str, Any]) -> Dict[str, Any]:
                    nonlocal completed_count
                    target_aid = asgn.get("id")
                    if not target_aid and asgn.get("url"):
                        m = re.search(r"/assignment/(\d+)", asgn["url"])
                        if m:
                            target_aid = m.group(1)

                    if not target_aid:
                        return asgn

                    url = urljoin(base_url, f"/assignment/{target_aid}/info")
                    max_retries = 3
                    base_delay = 0.35

                    for attempt in range(max_retries):
                        try:
                            resp = http_session.get(url, timeout=20)
                            if resp.status_code == 200:
                                details = parse_assignment_detail_html(resp.text, base_url=base_url)
                                if details.get("due_date"):
                                    asgn["due_date"] = details["due_date"]
                                if details.get("posted_date"):
                                    asgn["posted_date"] = details["posted_date"]
                                if details.get("category"):
                                    asgn["category"] = details["category"]
                                if details.get("grading_period"):
                                    asgn["grading_period"] = details["grading_period"]
                                if details.get("description"):
                                    asgn["description"] = details["description"]
                                if details.get("description_html"):
                                    asgn["description_html"] = details["description_html"]
                                asgn["attachments"] = details.get("attachments", [])
                                asgn["submissions"] = details.get("submissions", [])
                                asgn["comments_count"] = details.get("comments_count", 0)
                                break
                            elif resp.status_code == 429:
                                retry_after = resp.headers.get("Retry-After")
                                delay = float(retry_after) if retry_after and retry_after.isdigit() else (base_delay * (1.5 ** attempt))
                                if print_logs:
                                    print(f"Rate limited (HTTP 429) for #{target_aid}. Retrying in {delay:.2f}s...", file=sys.stderr, flush=True)
                                time.sleep(delay)
                            else:
                                if print_logs:
                                    print(f"Warning: HTTP {resp.status_code} for assignment {target_aid}", file=sys.stderr, flush=True)
                                break
                        except Exception as e:
                            if attempt == max_retries - 1 and print_logs:
                                print(f"Warning: Failed to fetch {target_aid} via HTTP: {e}", file=sys.stderr, flush=True)
                            time.sleep(base_delay)

                    with counter_lock:
                        completed_count += 1
                        if print_logs:
                            print(f"[{completed_count}/{len(assignments)}] Loaded {asgn.get('title')} (#{target_aid})...", file=sys.stderr, flush=True)

                    return asgn

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    list(executor.map(_fetch_single_assignment, assignments))

            else:
                if print_logs:
                    print(f"Fetching full details for {len(assignments)} assignments sequentially...", file=sys.stderr, flush=True)

                for i, asgn in enumerate(assignments):
                    target_aid = asgn.get("id")
                    if not target_aid and asgn.get("url"):
                        m = re.search(r"/assignment/(\d+)", asgn["url"])
                        if m:
                            target_aid = m.group(1)

                    if not target_aid:
                        continue

                    if print_logs:
                        print(f"[{i+1}/{len(assignments)}] Loading {asgn.get('title')} (#{target_aid})...", file=sys.stderr, flush=True)

                    try:
                        details = fetch_assignment_details(target_aid, base_url=base_url, print_logs=False, page=p)
                        if details.get("due_date"):
                            asgn["due_date"] = details["due_date"]
                        if details.get("posted_date"):
                            asgn["posted_date"] = details["posted_date"]
                        if details.get("category"):
                            asgn["category"] = details["category"]
                        if details.get("grading_period"):
                            asgn["grading_period"] = details["grading_period"]
                        if details.get("description"):
                            asgn["description"] = details["description"]
                        if details.get("description_html"):
                            asgn["description_html"] = details["description_html"]
                        asgn["attachments"] = details.get("attachments", [])
                        asgn["submissions"] = details.get("submissions", [])
                        asgn["comments_count"] = details.get("comments_count", 0)
                    except Exception as e:
                        if print_logs:
                            print(f"Warning: Failed to fetch full details for {target_aid}: {e}", file=sys.stderr, flush=True)

        return {
            "course_id": course_id,
            "course_title": course_title,
            "course_url": urljoin(base_url, f"/course/{course_id}/materials"),
            "total_assignments": len(assignments),
            "assignments": assignments,
        }

    if page is not None:
        return _fetch(page)

    with get_browser_session(base_url=base_url, profile_dir=profile_dir, headless=headless, timeout_seconds=timeout_seconds, print_logs=print_logs) as (_, p):
        return _fetch(p)


# ---------------------------------------------------------------------------
# CLI & Terminal Interface
# ---------------------------------------------------------------------------

def _render_rich_table(data: Dict[str, Any]):
    """
    Renders formatted terminal output using rich.
    """
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    course_title = data.get("course_title", "Course")
    assignments = data.get("assignments", [])

    console.print(Panel.fit(
        f"[bold blue]{course_title}[/bold blue] (ID: {data.get('course_id')})\n"
        f"[green]Assignments Found: {len(assignments)}[/green]",
        title="Schoology Assignments",
        border_style="blue"
    ))

    if not assignments:
        console.print("[yellow]No assignments found for this course.[/yellow]")
        return

    for i, asgn in enumerate(assignments, 1):
        category = asgn.get("category") or "Assignment"
        title = asgn.get("title") or "Untitled"
        due = asgn.get("due_date")
        due_info = f" (Due: {due})" if due else ""

        subs = asgn.get("submissions", [])
        if subs:
            status_parts = [f"{s.get('status', 'Submitted')} ({s.get('submitted_at', '')})".strip() for s in subs]
            status_str = f" [bold green][{', '.join(status_parts)}][/bold green]"
        else:
            status_str = ""

        atts = asgn.get("attachments", [])
        att_info = f" [dim]({len(atts)} attachment{'s' if len(atts) > 1 else ''})[/dim]" if atts else ""

        padding = " " * (len(str(len(assignments))) - len(str(i)))
        console.print(f"  ({i}){padding} [bold blue][{category}][/bold blue] {title}{due_info}{status_str}{att_info}")

    console.print("")


def main():
    parser = argparse.ArgumentParser(
        description="Schoology Assignment Fetcher: retrieves assignments data for a given course."
    )
    parser.add_argument(
        "--course",
        "-c",
        help="Target course ID, title substring (e.g. 'Java', 'Algebra'), or course URL",
    )
    parser.add_argument(
        "--list-courses",
        "-l",
        action="store_true",
        help="List all enrolled courses and exit",
    )
    parser.add_argument(
        "--assignment",
        "-a",
        help="Directly fetch details for a single assignment by ID or URL",
    )
    parser.add_argument(
        "--fast",
        "--no-details",
        action="store_false",
        dest="fetch_details",
        default=True,
        help="Fast mode: extract assignments from materials list only without loading each assignment page",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON to stdout",
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
        help="Open visible browser window",
    )
    parser.add_argument(
        "--print-logs",
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Toggle printing status logs to stderr (default: True)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_false",
        dest="print_logs",
        help="Suppress status messages on stderr",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=2,
        help="Number of concurrent workers for parallel fetching (default: 2)",
    )
    parser.add_argument(
        "--parallel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use parallel HTTP worker pool for fetching assignment details (default: True, use --no-parallel for sequential)",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_BASE_URL,
        help=f"Schoology base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE_DIR,
        help="Browser profile directory",
    )

    args = parser.parse_args()

    try:
        # 1. Fetch single assignment details
        if args.assignment:
            details = fetch_assignment_details(
                args.assignment,
                base_url=args.url,
                profile_dir=args.profile,
                headless=args.headless,
                print_logs=args.print_logs,
            )
            if args.json:
                print(json.dumps(details, indent=2))
            else:
                from rich.console import Console
                from rich.panel import Panel
                console = Console()

                while True:
                    attachments = details.get("attachments", [])
                    submissions = details.get("submissions", [])
                    openable_links = []

                    if attachments:
                        att_lines = []
                        for idx, a in enumerate(attachments, 1):
                            open_url = a.get("viewer_url") or a.get("url")
                            key_prefix = f"a{idx}"
                            if open_url:
                                openable_links.append((key_prefix, a.get("title", f"Attachment {idx}"), open_url))
                                att_lines.append(f"  [{key_prefix}] [bold cyan]{a['title']}[/bold cyan] ({a.get('size') or 'file'})")
                            else:
                                att_lines.append(f"  • {a['title']} ({a.get('size') or 'file'})")
                        atts_desc = "\n".join(att_lines)
                    else:
                        atts_desc = "  None"

                    if submissions:
                        sub_lines = []
                        for idx, s in enumerate(submissions, 1):
                            open_url = s.get("url")
                            key_prefix = f"s{idx}"
                            summary_str = s.get("summary") or s.get("status") or "Submission"
                            submitted_str = f" on {s.get('submitted_at')}" if s.get("submitted_at") else ""
                            if open_url:
                                openable_links.append((key_prefix, f"Submission revision {idx}", open_url))
                                sub_lines.append(f"  [{key_prefix}] [bold green]{summary_str}[/bold green]{submitted_str}")
                            else:
                                sub_lines.append(f"  • {summary_str}{submitted_str}")
                        subs_desc = "\n".join(sub_lines)
                    else:
                        subs_desc = "  None"

                    console.print(Panel.fit(
                        f"[bold blue]{details.get('title')}[/bold blue] (#{details.get('id')})\n"
                        f"[yellow]Due Date:[/yellow] {details.get('due_date') or 'No due date'}\n"
                        f"[magenta]Category:[/magenta] {details.get('category') or 'Uncategorized'}\n"
                        f"[cyan]Grading Period:[/cyan] {details.get('grading_period') or 'N/A'}\n"
                        f"[white]Posted Date:[/white] {details.get('posted_date') or 'N/A'}\n\n"
                        f"[bold]Description / Instructions:[/bold]\n{details.get('description') or 'No description provided.'}\n\n"
                        f"[bold]Attachments (Received):[/bold]\n{atts_desc}\n\n"
                        f"[bold]Submissions:[/bold]\n{subs_desc}",
                        title="Assignment Detail",
                        border_style="cyan",
                    ))
                    console.print("")

                    if openable_links:
                        options_display = ", ".join([f"'{k}' for {lbl}" for k, lbl, _ in openable_links])
                        console.print(f"[bold green]Open link in browser ({options_display}), or hit Enter to exit:[/bold green] ", end="")
                    else:
                        break

                    try:
                        action = input().strip().lower()
                    except (KeyboardInterrupt, EOFError):
                        break

                    if not action:
                        break

                    target_link = next((url for k, _, url in openable_links if k.lower() == action), None)
                    if target_link:
                        console.print(f"[bold green]Opening in browser: {target_link}[/bold green]\n")
                        webbrowser.open(target_link)
                    else:
                        console.print(f"[bold red]Unknown option '{action}'.[/bold red]\n")
            return

        # 2. List courses
        if args.list_courses:
            courses = fetch_courses(
                base_url=args.url,
                profile_dir=args.profile,
                headless=args.headless,
                print_logs=args.print_logs,
            )
            if args.json:
                print(json.dumps(courses, indent=2))
            else:
                from rich.console import Console
                from rich.table import Table
                console = Console()
                table = Table(title="Enrolled Schoology Courses")
                table.add_column("#", style="dim")
                table.add_column("Course ID", style="cyan")
                table.add_column("Title", style="bold green")
                table.add_column("Section", style="white")
                for i, c in enumerate(courses):
                    table.add_row(str(i), str(c.get("id")), c.get("title", ""), c.get("section", ""))
                console.print(table)
            return

        # 3. Interactive course selection if --course not provided
        target_course = args.course
        if not target_course:
            with get_browser_session(base_url=args.url, profile_dir=args.profile, headless=args.headless, print_logs=args.print_logs) as (_, page):
                courses = fetch_courses(base_url=args.url, print_logs=args.print_logs, page=page)
                if not courses:
                    print("No courses found.", file=sys.stderr)
                    sys.exit(1)

                from rich.console import Console
                console = Console()
                console.print("\n[bold blue]Select a Course to fetch assignments:[/bold blue]")
                for i, c in enumerate(courses):
                    console.print(f" ({i}) [bold cyan]{c['title']}[/bold cyan] [dim]({c['section']})[/dim] [white]ID: {c['id']}[/white]")

                choice = input("\nEnter course index or ID: ").strip()
                if choice.isdigit() and int(choice) < len(courses):
                    chosen = courses[int(choice)]
                else:
                    chosen = choice

                result = fetch_course_assignments(
                    course=chosen,
                    fetch_details=args.fetch_details,
                    parallel=args.parallel,
                    max_workers=args.workers,
                    base_url=args.url,
                    print_logs=args.print_logs,
                    page=page,
                )
        else:
            result = fetch_course_assignments(
                course=target_course,
                fetch_details=args.fetch_details,
                parallel=args.parallel,
                max_workers=args.workers,
                base_url=args.url,
                profile_dir=args.profile,
                headless=args.headless,
                print_logs=args.print_logs,
            )

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _render_rich_table(result)

    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
